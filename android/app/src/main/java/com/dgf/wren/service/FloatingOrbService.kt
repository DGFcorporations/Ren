package com.dgf.wren.service

import android.app.Service
import android.app.AlertDialog
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.PixelFormat
import android.os.Build
import android.os.IBinder
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import com.dgf.wren.net.WrenApiClient
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject

class FloatingOrbService : Service() {

    private lateinit var windowManager: WindowManager
    private var overlayView: FrameLayout? = null
    private val scope = CoroutineScope(Dispatchers.Main + Job())
    private lateinit var apiClient: WrenApiClient
    private var webSocket: WebSocket? = null
    private var orbView: TextView? = null
    private var pendingDraftText: String? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        windowManager = getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val prefs = getSharedPreferences("wren_prefs", Context.MODE_PRIVATE)
        val host = prefs.getString("host_url", "http://192.168.4.28:8765") ?: "http://192.168.4.28:8765"
        apiClient = WrenApiClient(host)
        createFloatingView()
        initWebSocket()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        intent?.getStringExtra("host_url")?.let { host ->
            apiClient.updateBaseUrl(host)
            initWebSocket()
        }
        return START_STICKY
    }

    private fun initWebSocket() {
        webSocket?.close(1000, "Reconnecting")
        webSocket = apiClient.connectWebSocket(object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                scope.launch {
                    setOrbStatus(Color.parseColor("#38BDF8"), "W")
                }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    val json = JSONObject(text)
                    when (json.optString("type")) {
                        "state_changed" -> {
                            val state = json.optString("state")
                            scope.launch {
                                when (state) {
                                    "LISTENING" -> setOrbStatus(Color.parseColor("#EF4444"), "●")
                                    "PROCESSING" -> setOrbStatus(Color.parseColor("#F59E0B"), "◌")
                                    "DRAFT_READY" -> setOrbStatus(Color.parseColor("#10B981"), "✓")
                                    else -> setOrbStatus(Color.parseColor("#38BDF8"), "W")
                                }
                            }
                        }
                        "draft_ready" -> {
                            val draft = json.optJSONObject("draft")
                            val textContent = draft?.optString("text") ?: ""
                            scope.launch {
                                setOrbStatus(Color.parseColor("#10B981"), "✓")
                                if (textContent.isNotBlank()) {
                                    pendingDraftText = textContent
                                    Toast.makeText(
                                        applicationContext,
                                        "Draft ready. Tap Wren to review before copying.",
                                        Toast.LENGTH_LONG
                                    ).show()
                                }
                            }
                        }
                    }
                } catch (e: Exception) {
                    android.util.Log.e("FloatingOrbService", "WebSocket parse error", e)
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                scope.launch {
                    setOrbStatus(Color.parseColor("#64748B"), "!")
                }
            }
        })
    }

    private fun setOrbStatus(color: Int, text: String) {
        orbView?.setBackgroundColor(color)
        orbView?.text = text
    }

    private fun createFloatingView() {
        val layoutType = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        } else {
            @Suppress("DEPRECATION")
            WindowManager.LayoutParams.TYPE_PHONE
        }

        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            layoutType,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = 100
            y = 200
        }

        val container = FrameLayout(this)
        val orb = TextView(this).apply {
            text = "W"
            setTextColor(Color.WHITE)
            textSize = 20f
            gravity = Gravity.CENTER
            setBackgroundColor(Color.parseColor("#38BDF8"))
            val size = (56 * resources.displayMetrics.density).toInt()
            layoutParams = FrameLayout.LayoutParams(size, size)
        }
        orbView = orb
        container.addView(orb)

        container.setOnTouchListener(object : View.OnTouchListener {
            private var initialX = 0
            private var initialY = 0
            private var touchX = 0f
            private var touchY = 0f
            private var isClick = true

            override fun onTouch(v: View?, event: MotionEvent?): Boolean {
                if (event == null) return false
                when (event.action) {
                    MotionEvent.ACTION_DOWN -> {
                        initialX = params.x
                        initialY = params.y
                        touchX = event.rawX
                        touchY = event.rawY
                        isClick = true
                        return true
                    }
                    MotionEvent.ACTION_MOVE -> {
                        val dx = (event.rawX - touchX).toInt()
                        val dy = (event.rawY - touchY).toInt()
                        if (Math.abs(dx) > 10 || Math.abs(dy) > 10) {
                            isClick = false
                        }
                        params.x = initialX + dx
                        params.y = initialY + dy
                        windowManager.updateViewLayout(container, params)
                        return true
                    }
                    MotionEvent.ACTION_UP -> {
                        if (isClick) {
                            onOrbClicked()
                        }
                        return true
                    }
                }
                return false
            }
        })

        overlayView = container
        windowManager.addView(container, params)
    }

    private fun onOrbClicked() {
        val draft = pendingDraftText
        if (!draft.isNullOrBlank()) {
            showDraftPreview(draft)
            return
        }

        val a11y = WrenAccessibilityService.instance
        val screenCtx = a11y?.extractScreenContext() ?: "No screen context"
        Toast.makeText(this, "Screen: ${screenCtx.take(40)}...", Toast.LENGTH_SHORT).show()

        // Send start/stop trigger over WebSocket or HTTP
        webSocket?.let { ws ->
            val payload = JSONObject().put("action", "ping").toString()
            ws.send(payload)
        }
    }

    private fun showDraftPreview(text: String) {
        val density = resources.displayMetrics.density
        val preview = TextView(this).apply {
            this.text = text
            textSize = 16f
            setTextIsSelectable(true)
            setPadding((16 * density).toInt(), (12 * density).toInt(), (16 * density).toInt(), (12 * density).toInt())
        }
        val scrollView = ScrollView(this).apply {
            addView(
                preview,
                ViewGroup.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT
                )
            )
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                (280 * density).toInt()
            )
        }

        val dialog = AlertDialog.Builder(this)
            .setTitle("Review draft")
            .setMessage("Review the full text. Copy does not paste into another app.")
            .setView(scrollView)
            .setNegativeButton("Close", null)
            .setPositiveButton("Copy for review") { _, _ ->
                val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                clipboard.setPrimaryClip(ClipData.newPlainText("Wren reviewed draft", text))
                setOrbStatus(Color.parseColor("#818CF8"), "C")
                Toast.makeText(
                    applicationContext,
                    "Copied. Switch to the chosen app and paste manually.",
                    Toast.LENGTH_LONG
                ).show()
            }
            .create()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            dialog.window?.setType(WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY)
        } else {
            @Suppress("DEPRECATION")
            dialog.window?.setType(WindowManager.LayoutParams.TYPE_PHONE)
        }
        dialog.show()
    }

    override fun onDestroy() {
        super.onDestroy()
        webSocket?.close(1000, "Service destroyed")
        overlayView?.let { windowManager.removeView(it) }
    }
}
