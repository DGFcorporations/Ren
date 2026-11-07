package com.dgf.wren.service

import android.accessibilityservice.AccessibilityService
import android.os.Bundle
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

class WrenAccessibilityService : AccessibilityService() {

    companion object {
        private const val TAG = "WrenA11y"
        var instance: WrenAccessibilityService? = null
            private set
    }

    var currentPackageName: String = ""
        private set

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        Log.i(TAG, "Wren Accessibility Service connected.")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return
        event.packageName?.let {
            currentPackageName = it.toString()
        }
    }

    override fun onInterrupt() {
        Log.w(TAG, "Wren Accessibility Service interrupted.")
    }

    override fun onDestroy() {
        super.onDestroy()
        if (instance == this) {
            instance = null
        }
    }

    /**
     * Extracts readable text content currently visible on the screen.
     */
    fun extractScreenContext(): String {
        val root = rootInActiveWindow ?: return "App: $currentPackageName (No accessible window)"
        val sb = StringBuilder()
        sb.append("App: ").append(currentPackageName).append("\n")
        traverseNode(root, sb, 0)
        return sb.toString()
    }

    private fun traverseNode(node: AccessibilityNodeInfo, sb: StringBuilder, depth: Int) {
        if (depth > 6) return
        val text = node.text
        val desc = node.contentDescription
        if (!text.isNullOrBlank()) {
            sb.append("  ".repeat(depth)).append("- ").append(text).append("\n")
        } else if (!desc.isNullOrBlank()) {
            sb.append("  ".repeat(depth)).append("[desc: ").append(desc).append("]\n")
        }

        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            traverseNode(child, sb, depth + 1)
            child.recycle()
        }
    }

    /**
     * Safely pastes or sets text into the currently focused input field.
     */
    fun pasteTextIntoActive(text: String): Boolean {
        val root = rootInActiveWindow ?: return false
        val focused = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT) ?: return false

        // Try ACTION_SET_TEXT first
        val args = Bundle()
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        val success = focused.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
        if (!success) {
            // Fallback to ACTION_PASTE
            return focused.performAction(AccessibilityNodeInfo.ACTION_PASTE)
        }
        return true
    }
}
