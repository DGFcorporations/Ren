package com.dgf.wren.service

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.dgf.wren.net.WrenApiClient
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject

/**
 * Floating Ren orb. Tap to dictate. The card shows the text before anything is pasted.
 * Ask Ren talks back on the same card. Paste here only runs after that confirmation.
 */
class FloatingOrbService : Service() {

    private lateinit var windowManager: WindowManager
    private var overlayView: LinearLayout? = null
    private val scope = CoroutineScope(Dispatchers.Main + Job())
    private val mainHandler = Handler(Looper.getMainLooper())
    private lateinit var apiClient: WrenApiClient
    private var webSocket: WebSocket? = null
    private var orbView: TextView? = null
    private var captionView: TextView? = null
    private var cardView: LinearLayout? = null
    private var heardView: TextView? = null
    private var bodyView: TextView? = null
    private var speechRecognizer: SpeechRecognizer? = null
    private var listening = false
    private var alive = false
    private var pendingPaste: String = ""
    private var lastScreen: String = ""

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        alive = true
        windowManager = getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val prefs = getSharedPreferences("wren_prefs", Context.MODE_PRIVATE)
        val host = prefs.getString("host_url", WrenApiClient.DEFAULT_HOST) ?: WrenApiClient.DEFAULT_HOST
        val token = prefs.getString("auth_token", WrenApiClient.DEFAULT_TOKEN) ?: WrenApiClient.DEFAULT_TOKEN
        apiClient = WrenApiClient(host, token)
        startInForeground()
        createFloatingView()
        initWebSocket()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val host = intent?.getStringExtra("host_url")
        val token = intent?.getStringExtra("auth_token")
        if (!host.isNullOrBlank()) apiClient.updateBaseUrl(host)
        if (!token.isNullOrBlank()) apiClient.setAuthToken(token)
        if (host != null || token != null) initWebSocket()
        return START_STICKY
    }

    private fun startInForeground() {
        val channelId = "ren_orb"
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(channelId, "Ren", NotificationManager.IMPORTANCE_LOW)
            val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            manager.createNotificationChannel(channel)
        }
        val notification: Notification = NotificationCompat.Builder(this, channelId)
            .setContentTitle("Ren is listening for a tap")
            .setContentText("Dictation stays on this phone until you confirm a paste.")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(4402, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(4402, notification)
        }
    }

    private var socketGen = 0

    private fun initWebSocket() {
        val gen = ++socketGen
        webSocket?.close(1000, "Reconnecting")
        webSocket = apiClient.connectWebSocket(object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                scope.launch { setOrbStatus(Color.parseColor("#38BDF8"), "R") }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                if (gen != socketGen) return
                try {
                    val json = JSONObject(text)
                    when (json.optString("type")) {
                        "state_changed" -> scope.launch { paintState(json.optString("state")) }
                        "paste_preview", "draft_ready" -> {
                            val draft = json.optJSONObject("draft")
                            val elevated = draft?.optString("text").orEmpty()
                            val raw = json.optString("raw")
                            if (elevated.isNotEmpty()) {
                                scope.launch { showPreview(raw.ifBlank { elevated }, elevated) }
                            }
                        }
                        "assistant_message" -> {
                            val reply = json.optString("text")
                            scope.launch { showReply(reply) }
                        }
                        "screen_request" -> scope.launch { sendScreen() }
                        "error" -> scope.launch {
                            captionView?.text = json.optString("message").take(80)
                        }
                    }
                } catch (e: Exception) {
                    android.util.Log.e("RenOrb", "WebSocket parse error", e)
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (!alive || gen != socketGen) return
                scope.launch { setOrbStatus(Color.parseColor("#64748B"), "!") }
                mainHandler.postDelayed({
                    if (alive && gen == socketGen) initWebSocket()
                }, 3000)
            }
        })
    }

    private fun paintState(state: String) {
        when (state) {
            "LISTENING" -> setOrbStatus(Color.parseColor("#EF4444"), "●")
            "PROCESSING" -> setOrbStatus(Color.parseColor("#F59E0B"), "◌")
            "DRAFT_READY" -> setOrbStatus(Color.parseColor("#10B981"), "✓")
            else -> if (!listening) setOrbStatus(Color.parseColor("#38BDF8"), "R")
        }
    }

    private fun setOrbStatus(color: Int, text: String) {
        orbView?.setBackgroundColor(color)
        orbView?.text = text
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

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
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = 48
            y = 180
        }

        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
        }

        val orb = TextView(this).apply {
            text = "R"
            setTextColor(Color.WHITE)
            textSize = 20f
            gravity = Gravity.CENTER
            setBackgroundColor(Color.parseColor("#38BDF8"))
            val size = dp(56)
            layoutParams = LinearLayout.LayoutParams(size, size)
        }
        orbView = orb

        val caption = TextView(this).apply {
            setTextColor(Color.WHITE)
            textSize = 12f
            setBackgroundColor(Color.parseColor("#121826"))
            setPadding(dp(8), dp(4), dp(8), dp(4))
            visibility = View.GONE
        }
        captionView = caption

        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.parseColor("#121826"))
            setPadding(dp(12), dp(12), dp(12), dp(12))
            visibility = View.GONE
            layoutParams = LinearLayout.LayoutParams(dp(280), LinearLayout.LayoutParams.WRAP_CONTENT).apply {
                topMargin = dp(8)
            }
        }
        val heard = TextView(this).apply {
            setTextColor(Color.parseColor("#94A3B8"))
            textSize = 11f
        }
        val body = TextView(this).apply {
            setTextColor(Color.WHITE)
            textSize = 14f
            setPadding(0, dp(6), 0, dp(8))
        }
        heardView = heard
        bodyView = body
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            )
        }
        row.addView(actionButton("Paste here") { pasteConfirmed() })
        row.addView(actionButton("Ask Ren") { askRen() })
        row.addView(actionButton("Dismiss") { hideCard() })
        card.addView(heard)
        card.addView(body)
        card.addView(row)
        cardView = card

        column.addView(orb)
        column.addView(caption)
        column.addView(card)

        orb.setOnTouchListener(object : View.OnTouchListener {
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
                        if (kotlin.math.abs(dx) > 10 || kotlin.math.abs(dy) > 10) isClick = false
                        params.x = initialX + dx
                        params.y = initialY + dy
                        windowManager.updateViewLayout(column, params)
                        return true
                    }
                    MotionEvent.ACTION_UP -> {
                        if (isClick) onOrbClicked()
                        return true
                    }
                }
                return false
            }
        })

        overlayView = column
        windowManager.addView(column, params)
    }

    private fun actionButton(label: String, onClick: () -> Unit): Button {
        return Button(this).apply {
            text = label
            textSize = 11f
            isAllCaps = false
            setTextColor(Color.parseColor("#090D16"))
            setBackgroundColor(Color.parseColor("#38BDF8"))
            setTypeface(typeface, Typeface.BOLD)
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply {
                marginEnd = dp(4)
            }
            setOnClickListener { onClick() }
        }
    }

    private fun onOrbClicked() {
        if (listening) {
            speechRecognizer?.stopListening()
            return
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            Toast.makeText(this, "Grant microphone in the Ren app first.", Toast.LENGTH_LONG).show()
            return
        }
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            Toast.makeText(this, "Dictation is not available on this phone.", Toast.LENGTH_LONG).show()
            return
        }
        sendScreen()
        startDictation()
    }

    private fun sendScreen() {
        val a11y = WrenAccessibilityService.instance
        lastScreen = a11y?.extractScreenContext() ?: "No screen access"
        val payload = JSONObject()
            .put("action", "screen_context")
            .put("package", a11y?.currentPackageName ?: "")
            .put("text", lastScreen.take(8000))
        webSocket?.send(payload.toString())
    }

    private fun startDictation() {
        speechRecognizer?.destroy()
        speechRecognizer = SpeechRecognizer.createSpeechRecognizer(this).also { recognizer ->
            recognizer.setRecognitionListener(object : RecognitionListener {
                override fun onReadyForSpeech(params: Bundle?) {
                    listening = true
                    setOrbStatus(Color.parseColor("#EF4444"), "●")
                    captionView?.visibility = View.VISIBLE
                    captionView?.text = "Listening…"
                }

                override fun onBeginningOfSpeech() {}
                override fun onRmsChanged(rmsdB: Float) {}
                override fun onBufferReceived(buffer: ByteArray?) {}
                override fun onEndOfSpeech() {
                    listening = false
                    setOrbStatus(Color.parseColor("#F59E0B"), "◌")
                }

                override fun onError(error: Int) {
                    listening = false
                    setOrbStatus(Color.parseColor("#38BDF8"), "R")
                    captionView?.text = "Dictation error $error"
                }

                override fun onResults(results: Bundle?) {
                    listening = false
                    val said = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull().orEmpty()
                    if (said.isBlank()) {
                        captionView?.text = "Didn't catch that"
                        setOrbStatus(Color.parseColor("#38BDF8"), "R")
                        return
                    }
                    captionView?.text = said
                    sendDictation(said)
                }

                override fun onPartialResults(partialResults: Bundle?) {
                    val partial = partialResults
                        ?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                        ?.firstOrNull()
                        .orEmpty()
                    if (partial.isNotBlank()) captionView?.text = partial
                }

                override fun onEvent(eventType: Int, params: Bundle?) {}
            })
        }
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
        }
        speechRecognizer?.startListening(intent)
    }

    private fun sendDictation(said: String) {
        val payload = JSONObject()
            .put("action", "dictate")
            .put("text", said)
            .put("source", "android")
            .put("package", WrenAccessibilityService.instance?.currentPackageName ?: "")
            .put("screen", lastScreen.take(8000))
        val sent = webSocket?.send(payload.toString()) ?: false
        if (!sent) {
            showPreview(said, said)
            captionView?.text = "Workstation offline. Preview is local."
        }
    }

    private fun showPreview(raw: String, elevated: String) {
        pendingPaste = elevated
        heardView?.text = "Heard: $raw"
        bodyView?.text = elevated
        cardView?.visibility = View.VISIBLE
        captionView?.visibility = View.VISIBLE
        captionView?.text = "Confirm before paste"
        setOrbStatus(Color.parseColor("#10B981"), "✓")
    }

    private fun showReply(reply: String) {
        if (reply.isBlank()) return
        pendingPaste = reply
        heardView?.text = "Ren"
        bodyView?.text = reply
        cardView?.visibility = View.VISIBLE
        captionView?.text = "Reply ready. Paste only if you want it."
        setOrbStatus(Color.parseColor("#818CF8"), "R")
    }

    private fun hideCard() {
        cardView?.visibility = View.GONE
        pendingPaste = ""
    }

    private fun askRen() {
        val text = pendingPaste.ifBlank { captionView?.text?.toString().orEmpty() }
        if (text.isBlank()) return
        val payload = JSONObject()
            .put("action", "converse")
            .put("text", text)
            .put("screen", lastScreen.take(8000))
            .put("package", WrenAccessibilityService.instance?.currentPackageName ?: "")
        val sent = webSocket?.send(payload.toString()) ?: false
        if (!sent) {
            showReply("The workstation is offline, so I can't answer from here yet.")
        } else {
            captionView?.text = "Asking Ren…"
            setOrbStatus(Color.parseColor("#F59E0B"), "◌")
        }
    }

    private fun pasteConfirmed() {
        val text = pendingPaste
        if (text.isBlank()) {
            Toast.makeText(this, "Nothing to paste yet.", Toast.LENGTH_SHORT).show()
            return
        }
        val a11y = WrenAccessibilityService.instance
        if (a11y == null) {
            Toast.makeText(this, "Turn on Ren accessibility, then focus a text field.", Toast.LENGTH_LONG).show()
            return
        }
        val ok = a11y.pasteText(text)
        if (ok) {
            Toast.makeText(this, "Pasted.", Toast.LENGTH_SHORT).show()
            hideCard()
        } else {
            Toast.makeText(this, "No focused field. Tap a text box and press Paste here again.", Toast.LENGTH_LONG).show()
        }
    }

    override fun onDestroy() {
        alive = false
        speechRecognizer?.destroy()
        webSocket?.close(1000, "Service destroyed")
        overlayView?.let {
            try {
                windowManager.removeView(it)
            } catch (_: Exception) {
            }
        }
        super.onDestroy()
    }
}
