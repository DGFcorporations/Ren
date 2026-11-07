package com.dgf.wren.net

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

class WrenApiClient(private var baseUrl: String = "http://10.0.2.2:8765") {

    private val client = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private val jsonMedia = "application/json; charset=utf-8".toMediaType()

    fun updateBaseUrl(newUrl: String) {
        baseUrl = newUrl.trimEnd('/')
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
}
