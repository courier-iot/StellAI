package com.stellai.app

/** A plain-language message for something that went wrong talking to the server. */
fun friendlyErrorText(e: Exception, serverUrl: String): String {
    val msg = e.message.orEmpty()
    return when {
        e is java.net.ConnectException || msg.contains("Failed to connect") ->
            "Can't reach the server at $serverUrl. Is the server running, and is the phone on the same Wi-Fi or Tailscale?"
        // "connect timed out": nothing answered at that address at all (on a phone, usually Tailscale being off)
        e is java.net.SocketTimeoutException && msg.contains("connect", ignoreCase = true) ->
            "Can't reach the server at $serverUrl. Is Tailscale on (or the phone on the same Wi-Fi), and is the server running?"
        e is java.net.SocketTimeoutException -> "The server took too long to respond."
        e is java.net.UnknownHostException -> "That server address doesn't look right. Check Settings."
        msg.contains("HTTP 401") -> "The API key was rejected. Copy it again from the server's terminal into Settings."
        msg.contains("HTTP 404") -> "The server doesn't have this yet. It updates itself within about 30 minutes when " +
            "Stella isn't busy; to update now, tap Check for updates on the Server screen."
        msg.contains("image input is not supported", ignoreCase = true) || msg.contains("mmproj", ignoreCase = true) ->
            "Photo understanding isn't installed on the server. Run the setup on the server again and turn on the vision add-on."
        else -> msg.ifBlank { e.javaClass.simpleName }
    }
}
