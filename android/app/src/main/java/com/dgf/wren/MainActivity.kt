package com.dgf.wren

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.dgf.wren.net.WrenApiClient
import com.dgf.wren.service.FloatingOrbService
import com.dgf.wren.service.WrenAccessibilityService
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {

    private val PERM_REQ_AUDIO = 1001
    private val PERM_REQ_NOTIF = 1002
    private val PREFS_NAME = "wren_prefs"
    private val PREF_HOST = "host_url"
    private val PREF_TOKEN = "auth_token"
    private val PREF_MODE = "active_mode"

    private lateinit var apiClient: WrenApiClient

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val savedHost = prefs.getString(PREF_HOST, "") ?: ""
        val savedToken = WrenApiClient.usableToken(prefs.getString(PREF_TOKEN, ""))

        apiClient = WrenApiClient(savedHost, savedToken)

        val editHost = findViewById<EditText>(R.id.edit_host)
        val editToken = findViewById<EditText>(R.id.edit_token)
        editHost.setText(savedHost)
        editToken.setText(savedToken)

        val btnModeElevate = findViewById<Button>(R.id.btn_mode_elevate)
        val btnModeAgent = findViewById<Button>(R.id.btn_mode_agent)
        val btnModeBrainstorm = findViewById<Button>(R.id.btn_mode_brainstorm)

        val btnOverlay = findViewById<Button>(R.id.btn_overlay)
        val btnA11y = findViewById<Button>(R.id.btn_a11y)
        val btnMic = findViewById<Button>(R.id.btn_mic)
        val btnStartOrb = findViewById<Button>(R.id.btn_start_orb)

        fun rememberConnection(): Boolean {
            val host = editHost.text.toString().trim()
            val token = WrenApiClient.usableToken(editToken.text.toString())
            if (host.isEmpty() || token.isEmpty()) {
                Toast.makeText(
                    this,
                    "Enter the workstation address and the pairing token",
                    Toast.LENGTH_LONG
                ).show()
                return false
            }
            prefs.edit().putString(PREF_HOST, host).putString(PREF_TOKEN, token).apply()
            apiClient.updateBaseUrl(host)
            apiClient.setAuthToken(token)
            return true
        }

        fun handleSetMode(mode: String) {
            if (!rememberConnection()) return
            prefs.edit().putString(PREF_MODE, mode).apply()
            lifecycleScope.launch {
                val res = apiClient.setMode(mode)
                if (res.isSuccess) {
                    Toast.makeText(this@MainActivity, "Active Mode: $mode", Toast.LENGTH_SHORT).show()
                } else {
                    Toast.makeText(this@MainActivity, "Failed to reach desktop: ${res.exceptionOrNull()?.message}", Toast.LENGTH_LONG).show()
                }
            }
        }

        btnModeElevate.setOnClickListener { handleSetMode("elevate") }
        btnModeAgent.setOnClickListener { handleSetMode("agent") }
        btnModeBrainstorm.setOnClickListener { handleSetMode("brainstorm") }

        btnOverlay.setOnClickListener {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !Settings.canDrawOverlays(this)) {
                val intent = Intent(
                    Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    Uri.parse("package:$packageName")
                )
                startActivity(intent)
            } else {
                Toast.makeText(this, "Overlay permission already granted!", Toast.LENGTH_SHORT).show()
            }
        }

        btnA11y.setOnClickListener {
            val intent = Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
            startActivity(intent)
            Toast.makeText(this, "Enable Ren in Accessibility settings", Toast.LENGTH_LONG).show()
        }

        btnMic.setOnClickListener {
            val needs = mutableListOf(Manifest.permission.RECORD_AUDIO)
            if (Build.VERSION.SDK_INT >= 33) {
                needs.add(Manifest.permission.POST_NOTIFICATIONS)
            }
            val missing = needs.filter {
                ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
            }
            if (missing.isNotEmpty()) {
                ActivityCompat.requestPermissions(this, missing.toTypedArray(), PERM_REQ_AUDIO)
            } else {
                Toast.makeText(this, "Microphone permission already granted!", Toast.LENGTH_SHORT).show()
            }
        }

        btnStartOrb.setOnClickListener {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !Settings.canDrawOverlays(this)) {
                Toast.makeText(this, "Please grant Overlay permission first!", Toast.LENGTH_LONG).show()
                return@setOnClickListener
            }
            if (Build.VERSION.SDK_INT >= 33 &&
                ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
            ) {
                ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), PERM_REQ_NOTIF)
            }
            rememberConnection()
            val host = editHost.text.toString().trim()
            val token = WrenApiClient.usableToken(editToken.text.toString())
            if (host.isEmpty() || token.isEmpty()) return@setOnClickListener

            val intent = Intent(this, FloatingOrbService::class.java).apply {
                putExtra("host_url", host)
                putExtra("auth_token", token)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(intent)
            } else {
                startService(intent)
            }
            Toast.makeText(this, "Ren orb is on. Tap it to dictate.", Toast.LENGTH_SHORT).show()
        }
    }

    override fun onResume() {
        super.onResume()
        updateStatus()
    }

    private fun updateStatus() {
        val statusText = findViewById<TextView>(R.id.status_text)
        val hasOverlay = Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(this)
        val hasA11y = WrenAccessibilityService.instance != null
        val hasMic = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

        val sb = StringBuilder("=== Service Status ===\n")
        sb.append("• Overlay Permission: ").append(if (hasOverlay) "READY" else "NEEDED").append("\n")
        sb.append("• Accessibility Service: ").append(if (hasA11y) "ACTIVE" else "DISABLED").append("\n")
        sb.append("• Microphone Access: ").append(if (hasMic) "GRANTED" else "NEEDED").append("\n")

        statusText.text = sb.toString()
    }
}
