package com.dgf.wren.net

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit

class WrenApiClient(
    private var baseUrl: String = "http://192.168.4.28:8765",
    private var authToken: String = "wren-pilot-auth-2026-dgf"
) {

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

    suspend fun getConnectInfo(): Result<JSONObject> = withContext(Dispatchers.IO) {
        try {
            val req = Request.Builder().url("$baseUrl/api/connect").get().build()
            val resp = client.newCall(req).execute()
            val body = resp.body?.string() ?: "{}"
            Result.success(JSONObject(body))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun getStatus(): Result<JSONObject> = withContext(Dispatchers.IO) {
        try {
            val req = Request.Builder().url("$baseUrl/api/status").get().build()
            val resp = client.newCall(req).execute()
            val body = resp.body?.string() ?: "{}"
            Result.success(JSONObject(body))
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
            val resp = client.newCall(req).execute()
            val body = resp.body?.string() ?: "{}"
            Result.success(body)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun searchFiles(query: String): Result<String> = withContext(Dispatchers.IO) {
        try {
            val req = Request.Builder().url("$baseUrl/api/files/search?q=$query").get().build()
            val resp = client.newCall(req).execute()
            val body = resp.body?.string() ?: "{}"
            Result.success(body)
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
            val resp = client.newCall(req).execute()
            val body = resp.body?.string() ?: "{}"
            Result.success(body)
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
            val resp = client.newCall(req).execute()
            Result.success(resp.isSuccessful)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * Connect to the workstation WebSocket endpoint (/ws/connect)
     * for real-time draft synchronization and audio streaming.
     */
    fun connectWebSocket(listener: WebSocketListener): WebSocket {
        val tokenParam = if (authToken.isNotBlank()) "?token=$authToken" else ""
        val wsUrl = baseUrl.replace("http://", "ws://").replace("https://", "wss://") + "/ws/connect" + tokenParam
        val request = Request.Builder().url(wsUrl).build()
        return client.newWebSocket(request, listener)
    }
}
