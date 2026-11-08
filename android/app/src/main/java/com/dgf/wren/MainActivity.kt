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
    private val PREFS_NAME = "wren_prefs"
    private val PREF_HOST = "host_url"
    private val PREF_MODE = "active_mode"

    private lateinit var apiClient: WrenApiClient

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val savedHost = prefs.getString(PREF_HOST, "http://192.168.4.28:8765") ?: "http://192.168.4.28:8765"
        val savedMode = prefs.getString(PREF_MODE, "elevate") ?: "elevate"

        apiClient = WrenApiClient(savedHost)

        val editHost = findViewById<EditText>(R.id.edit_host)
        editHost.setText(savedHost)

        val btnLan = findViewById<Button>(R.id.btn_preset_lan)
        val btnTailscale = findViewById<Button>(R.id.btn_preset_tailscale)
        val btnModeElevate = findViewById<Button>(R.id.btn_mode_elevate)
        val btnModeAgent = findViewById<Button>(R.id.btn_mode_agent)
        val btnModeBrainstorm = findViewById<Button>(R.id.btn_mode_brainstorm)

        val btnOverlay = findViewById<Button>(R.id.btn_overlay)
        val btnA11y = findViewById<Button>(R.id.btn_a11y)
        val btnMic = findViewById<Button>(R.id.btn_mic)
        val btnStartOrb = findViewById<Button>(R.id.btn_start_orb)

        btnLan.setOnClickListener {
            val url = "http://192.168.4.28:8765"
            editHost.setText(url)
            prefs.edit().putString(PREF_HOST, url).apply()
            apiClient.updateBaseUrl(url)
            Toast.makeText(this, "Set to LAN: $url", Toast.LENGTH_SHORT).show()
        }

        btnTailscale.setOnClickListener {
            val url = "http://100.90.174.74:8765"
            editHost.setText(url)
            prefs.edit().putString(PREF_HOST, url).apply()
            apiClient.updateBaseUrl(url)
            Toast.makeText(this, "Set to Tailscale: $url", Toast.LENGTH_SHORT).show()
        }

        fun handleSetMode(mode: String) {
            prefs.edit().putString(PREF_MODE, mode).apply()
            lifecycleScope.launch {
                val host = editHost.text.toString().trim()
                apiClient.updateBaseUrl(host)
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
            Toast.makeText(this, "Enable 'Wren Assistant' in Accessibility Settings", Toast.LENGTH_LONG).show()
        }

        btnMic.setOnClickListener {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.RECORD_AUDIO), PERM_REQ_AUDIO)
            } else {
                Toast.makeText(this, "Microphone permission already granted!", Toast.LENGTH_SHORT).show()
            }
        }

        btnStartOrb.setOnClickListener {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !Settings.canDrawOverlays(this)) {
                Toast.makeText(this, "Please grant Overlay permission first!", Toast.LENGTH_LONG).show()
                return@setOnClickListener
            }
            val host = editHost.text.toString().trim()
            prefs.edit().putString(PREF_HOST, host).apply()

            val intent = Intent(this, FloatingOrbService::class.java).apply {
                putExtra("host_url", host)
            }
            startService(intent)
            Toast.makeText(this, "Floating Wren Orb Started!", Toast.LENGTH_SHORT).show()
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
