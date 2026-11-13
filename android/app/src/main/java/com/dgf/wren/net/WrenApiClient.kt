package com.dgf.wren.net

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

class WrenApiClient(
    private var baseUrl: String = DEFAULT_HOST,
    private var authToken: String = DEFAULT_TOKEN
) {

    companion object {
        const val DEFAULT_HOST = ""
        const val DEFAULT_TOKEN = ""
        private const val RETIRED_TOKEN_SHA256 =
            "1843dd013bce8d1d9f0c9810a03895f67f0e718e016d2cfc81a2d28f8fbe1437"

        fun usableToken(saved: String?): String {
            val value = saved?.trim().orEmpty()
            if (value.isEmpty() || isRetiredToken(value)) return ""
            return value
        }

        fun isRetiredToken(token: String): Boolean {
            val digest = MessageDigest.getInstance("SHA-256")
                .digest(token.toByteArray(Charsets.UTF_8))
            val hex = buildString(digest.size * 2) {
                for (byte in digest) append("%02x".format(byte.toInt() and 0xff))
            }
            return hex == RETIRED_TOKEN_SHA256
        }
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .addInterceptor { chain ->
            val original = chain.request()
            val reqBuilder = original.newBuilder()
            if (authToken.isNotBlank()) {
                reqBuilder.header("Authorization", "Bearer $authToken")
            }
            chain.proceed(reqBuilder.build())
        }
        .build()

    private val jsonMedia = "application/json; charset=utf-8".toMediaType()

    fun updateBaseUrl(newUrl: String) {
        baseUrl = newUrl.trimEnd('/')
    }

    fun getBaseUrl(): String = baseUrl

    fun setAuthToken(token: String) {
        authToken = token.trim()
    }

    fun getAuthToken(): String = authToken

    private fun execute(req: Request): String {
        client.newCall(req).execute().use { resp ->
            val body = resp.body?.string() ?: ""
            if (!resp.isSuccessful) {
                throw IllegalStateException("HTTP ${resp.code}: ${body.take(180)}")
            }
            return body.ifBlank { "{}" }
        }
    }

    suspend fun getConnectInfo(): Result<JSONObject> = withContext(Dispatchers.IO) {
        try {
            val req = Request.Builder().url("$baseUrl/api/connect").get().build()
            Result.success(JSONObject(execute(req)))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun getStatus(): Result<JSONObject> = withContext(Dispatchers.IO) {
        try {
            val req = Request.Builder().url("$baseUrl/api/status").get().build()
            Result.success(JSONObject(execute(req)))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun setMode(mode: String): Result<String> = withContext(Dispatchers.IO) {
        try {
            val payload = JSONObject().put("mode", mode).toString()
            val req = Request.Builder()
                .url("$baseUrl/api/mode")
                .post(payload.toRequestBody(jsonMedia))
                .build()
            Result.success(execute(req))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun searchFiles(query: String): Result<String> = withContext(Dispatchers.IO) {
        try {
            val req = Request.Builder().url("$baseUrl/api/files/search?q=$query").get().build()
            Result.success(execute(req))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun readFile(path: String): Result<String> = withContext(Dispatchers.IO) {
        try {
            val payload = JSONObject().put("file_path", path).toString()
            val req = Request.Builder()
                .url("$baseUrl/api/files/read")
                .post(payload.toRequestBody(jsonMedia))
                .build()
            Result.success(execute(req))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun attachFile(path: String): Result<Boolean> = withContext(Dispatchers.IO) {
        try {
            val payload = JSONObject().put("file_path", path).toString()
            val req = Request.Builder()
                .url("$baseUrl/api/files/attach")
                .post(payload.toRequestBody(jsonMedia))
                .build()
            execute(req)
            Result.success(true)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * Connect to the workstation WebSocket endpoint (/ws/connect)
     * for dictation, conversation, and paste previews.
     */
    fun connectWebSocket(listener: WebSocketListener): WebSocket {
        val tokenParam = if (authToken.isNotBlank()) "?token=$authToken" else ""
        val wsUrl = baseUrl.replace("http://", "ws://").replace("https://", "wss://") + "/ws/connect" + tokenParam
        val request = Request.Builder().url(wsUrl).build()
        return client.newWebSocket(request, listener)
    }
}
