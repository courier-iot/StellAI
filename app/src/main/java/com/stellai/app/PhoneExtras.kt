package com.stellai.app

import android.Manifest
import android.app.AlarmManager
import android.app.NotificationManager
import android.app.SearchManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioManager
import android.media.MediaMetadata
import android.media.session.MediaController
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.provider.AlarmClock
import android.provider.CalendarContract
import android.provider.ContactsContract
import android.provider.MediaStore
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.view.KeyEvent
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * Lets Stella see notifications and control music players. Android only binds it after the user turns on
 * "Notification access" for StellAI (Settings → Phone access). Nothing is stored or sent anywhere except the
 * answers to what you ask Stella.
 */
class StellaNotificationListener : NotificationListenerService() {
    override fun onListenerConnected() {
        instance = this
    }

    override fun onListenerDisconnected() {
        if (instance === this) instance = null
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    companion object {
        @Volatile
        var instance: StellaNotificationListener? = null
    }
}

/**
 * The phone actions that need more than one intent: alarms and timers you already set, music, sound modes,
 * brightness, notifications, contacts, the calendar and quick-settings panels. Each returns (ok, message);
 * PhoneTools turns that into the chat line and the model's result.
 */
object PhoneExtras {

    // ---- Which special access StellAI has (shown in Settings → Phone access) -----------------------
    fun hasNotificationAccess(ctx: Context) =
        ctx.packageName in NotificationManagerCompat.getEnabledListenerPackages(ctx)

    fun hasDndAccess(ctx: Context) =
        (ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).isNotificationPolicyAccessGranted

    fun hasWriteSettings(ctx: Context) = Settings.System.canWrite(ctx)

    fun has(ctx: Context, permission: String) =
        ContextCompat.checkSelfPermission(ctx, permission) == PackageManager.PERMISSION_GRANTED

    private const val NEED_NOTIF = "Stella needs Notification access for this: StellAI Settings → Phone access → Notifications & music"
    private const val NEED_DND = "Stella needs Do Not Disturb access for this: StellAI Settings → Phone access → Do Not Disturb"
    private const val NEED_WRITE = "Stella needs permission to change system settings: StellAI Settings → Phone access → Brightness"

    data class Res(val ok: Boolean, val msg: String, val data: JSONObject? = null)

    // ---- Alarms and timers ---------------------------------------------------------------------
    /** Turns off alarms in the clock app. Android has no way for apps to list or delete alarms directly. */
    fun cancelAlarm(ctx: Context, args: JSONObject): Res {
        val time = args.optString("time", "").trim()
        val label = args.optString("label", "").trim()
        val which = args.optString("which", "").lowercase()
        val intent = Intent(AlarmClock.ACTION_DISMISS_ALARM)
        val what: String
        when {
            which == "all" -> {
                intent.putExtra(AlarmClock.EXTRA_ALARM_SEARCH_MODE, AlarmClock.ALARM_SEARCH_MODE_ALL)
                what = "all alarms"
            }
            time.isNotEmpty() -> {
                val m = Regex("(\\d{1,2})(?::(\\d{2}))?").find(time) ?: return Res(false, "Time must look like 07:30")
                val hour = m.groupValues[1].toInt()
                val minute = m.groupValues[2].ifEmpty { "0" }.toInt()
                if (hour !in 0..23 || minute !in 0..59) return Res(false, "Invalid time")
                intent.putExtra(AlarmClock.EXTRA_ALARM_SEARCH_MODE, AlarmClock.ALARM_SEARCH_MODE_TIME)
                    .putExtra(AlarmClock.EXTRA_HOUR, hour)
                    .putExtra(AlarmClock.EXTRA_MINUTES, minute)
                what = "the %d:%02d alarm".format(hour, minute)
            }
            label.isNotEmpty() -> {
                intent.putExtra(AlarmClock.EXTRA_ALARM_SEARCH_MODE, AlarmClock.ALARM_SEARCH_MODE_LABEL)
                    .putExtra(AlarmClock.EXTRA_MESSAGE, label)
                what = "the \"$label\" alarm"
            }
            else -> {
                intent.putExtra(AlarmClock.EXTRA_ALARM_SEARCH_MODE, AlarmClock.ALARM_SEARCH_MODE_NEXT)
                what = "the next alarm"
            }
        }
        if (start(ctx, intent)) return Res(true, "Asked the clock app to turn off $what (it may ask you to confirm)")
        // Some clock apps don't take requests: show the alarm list instead
        return if (start(ctx, Intent(AlarmClock.ACTION_SHOW_ALARMS)))
            Res(true, "This clock app doesn't take requests to turn alarms off, so I opened the alarm list for you")
        else Res(false, "No clock app found")
    }

    fun alarmInfo(ctx: Context): Res {
        val am = ctx.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val next = am.nextAlarmClock
        val data = JSONObject().put("note", "Android only tells apps the next alarm; it can't list them all")
        if (next == null) return Res(true, "No alarm is set", data.put("next_alarm", JSONObject.NULL))
        val t = Instant.ofEpochMilli(next.triggerTime).atZone(ZoneId.systemDefault())
        val text = t.format(DateTimeFormatter.ofPattern("EEE d MMM, h:mm a"))
        return Res(true, "Next alarm: $text", data.put("next_alarm", text))
    }

    fun openClock(ctx: Context, view: String): Res {
        val timers = view.lowercase().startsWith("timer")
        val action = if (timers) AlarmClock.ACTION_SHOW_TIMERS else AlarmClock.ACTION_SHOW_ALARMS
        return if (start(ctx, Intent(action))) Res(true, if (timers) "Opened your timers" else "Opened your alarms")
        else Res(false, "No clock app found")
    }

    fun cancelTimer(ctx: Context): Res {
        if (Build.VERSION.SDK_INT < 28) return openClock(ctx, "timers")
        return if (start(ctx, Intent(AlarmClock.ACTION_DISMISS_TIMER))) Res(true, "Asked the clock app to stop the timer")
        else openClock(ctx, "timers")
    }

    // ---- Music and other media -----------------------------------------------------------------
    private fun controllers(ctx: Context): List<MediaController> {
        if (!hasNotificationAccess(ctx)) return emptyList()
        return try {
            val msm = ctx.getSystemService(Context.MEDIA_SESSION_SERVICE) as MediaSessionManager
            msm.getActiveSessions(ComponentName(ctx, StellaNotificationListener::class.java))
        } catch (_: SecurityException) {
            emptyList()
        }
    }

    /** The player that's playing, else the most recent one. */
    private fun activeController(ctx: Context, app: String = ""): MediaController? {
        val all = controllers(ctx)
        if (app.isNotBlank()) {
            val pkg = findPackage(ctx, app)
            return all.firstOrNull { it.packageName == pkg }
        }
        return all.firstOrNull { it.playbackState?.state == PlaybackState.STATE_PLAYING } ?: all.firstOrNull()
    }

    private fun appLabel(ctx: Context, pkg: String): String = try {
        val pm = ctx.packageManager
        pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
    } catch (_: Exception) {
        pkg
    }

    fun mediaControl(ctx: Context, args: JSONObject): Res {
        val action = args.optString("action", "").lowercase().trim()
        val c = activeController(ctx, args.optString("app", ""))
        if (c != null) {
            val t = c.transportControls
            val playing = c.playbackState?.state == PlaybackState.STATE_PLAYING
            when (action) {
                "play" -> t.play()
                "pause" -> t.pause()
                "toggle", "play_pause" -> if (playing) t.pause() else t.play()
                "next", "skip" -> t.skipToNext()
                "previous", "back" -> t.skipToPrevious()
                "restart" -> t.seekTo(0)
                "stop" -> t.stop()
                "forward" -> t.seekTo((c.playbackState?.position ?: 0) + 30_000)
                "rewind" -> t.seekTo(maxOf(0, (c.playbackState?.position ?: 0) - 15_000))
                else -> return Res(false, "Unknown media action \"$action\"")
            }
            return Res(true, "${actionWord(action)} (${appLabel(ctx, c.packageName)})")
        }
        // No notification access: send the same key a headset button would; the last player used gets it
        val key = when (action) {
            "play" -> KeyEvent.KEYCODE_MEDIA_PLAY
            "pause" -> KeyEvent.KEYCODE_MEDIA_PAUSE
            "toggle", "play_pause" -> KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE
            "next", "skip" -> KeyEvent.KEYCODE_MEDIA_NEXT
            "previous", "back", "restart" -> KeyEvent.KEYCODE_MEDIA_PREVIOUS
            "stop" -> KeyEvent.KEYCODE_MEDIA_STOP
            "forward" -> KeyEvent.KEYCODE_MEDIA_FAST_FORWARD
            "rewind" -> KeyEvent.KEYCODE_MEDIA_REWIND
            else -> return Res(false, "Unknown media action \"$action\"")
        }
        val audio = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val now = SystemClock.uptimeMillis()
        audio.dispatchMediaKeyEvent(KeyEvent(now, now, KeyEvent.ACTION_DOWN, key, 0))
        audio.dispatchMediaKeyEvent(KeyEvent(now, now, KeyEvent.ACTION_UP, key, 0))
        return Res(true, actionWord(action) + " (sent to the last music app used)")
    }

    private fun actionWord(a: String) = when (a) {
        "play" -> "Playing"
        "pause" -> "Paused"
        "toggle", "play_pause" -> "Play/pause"
        "next", "skip" -> "Skipped to the next track"
        "previous", "back" -> "Back to the previous track"
        "restart" -> "Restarted the track"
        "stop" -> "Stopped"
        "forward" -> "Jumped ahead 30 seconds"
        "rewind" -> "Went back 15 seconds"
        else -> a
    }

    fun nowPlaying(ctx: Context): Res {
        if (!hasNotificationAccess(ctx)) return Res(false, NEED_NOTIF)
        val c = activeController(ctx) ?: return Res(true, "Nothing is playing")
        val md = c.metadata
        val title = md?.getString(MediaMetadata.METADATA_KEY_TITLE).orEmpty()
        val artist = md?.getString(MediaMetadata.METADATA_KEY_ARTIST)
            ?: md?.getString(MediaMetadata.METADATA_KEY_ALBUM_ARTIST).orEmpty()
        val album = md?.getString(MediaMetadata.METADATA_KEY_ALBUM).orEmpty()
        val state = when (c.playbackState?.state) {
            PlaybackState.STATE_PLAYING -> "playing"
            PlaybackState.STATE_PAUSED -> "paused"
            PlaybackState.STATE_BUFFERING -> "loading"
            else -> "stopped"
        }
        val app = appLabel(ctx, c.packageName)
        val data = JSONObject().put("title", title).put("artist", artist).put("album", album).put("state", state).put("app", app)
        val line = listOf(title, artist).filter { it.isNotBlank() }.joinToString(" by ").ifBlank { "Something" }
        return Res(true, "$line ($state in $app)", data)
    }

    /** Plays a song, artist, album or playlist by name in a music app (Spotify, YouTube Music, ...). */
    fun playMusic(ctx: Context, args: JSONObject): Res {
        val query = args.optString("query", "").trim()
        if (query.isEmpty()) return Res(false, "What should I play?")
        val kind = args.optString("kind", "").lowercase()
        val appName = args.optString("app", "").trim()
        val extras = Bundle().apply {
            putString(SearchManager.QUERY, query)
            when (kind) {
                "artist" -> {
                    putString(MediaStore.EXTRA_MEDIA_FOCUS, MediaStore.Audio.Artists.ENTRY_CONTENT_TYPE)
                    putString(MediaStore.EXTRA_MEDIA_ARTIST, query)
                }
                "album" -> {
                    putString(MediaStore.EXTRA_MEDIA_FOCUS, MediaStore.Audio.Albums.ENTRY_CONTENT_TYPE)
                    putString(MediaStore.EXTRA_MEDIA_ALBUM, query)
                }
                "song", "track" -> {
                    putString(MediaStore.EXTRA_MEDIA_FOCUS, "vnd.android.cursor.item/audio")
                    putString(MediaStore.EXTRA_MEDIA_TITLE, query)
                }
                "playlist" -> {
                    putString(MediaStore.EXTRA_MEDIA_FOCUS, "vnd.android.cursor.item/playlist")
                    putString("android.intent.extra.playlist", query)
                }
            }
        }
        // 1. The music app that's already open, if it takes search requests: plays without leaving Stella
        val c = activeController(ctx, appName)
        if (c != null && ((c.playbackState?.actions ?: 0L) and PlaybackState.ACTION_PLAY_FROM_SEARCH) != 0L) {
            c.transportControls.playFromSearch(query, extras)
            return Res(true, "Playing \"$query\" in ${appLabel(ctx, c.packageName)}")
        }
        // 2. Ask a music app to search and play (opens it). No app named: Stellar Disc if it's installed.
        val intent = Intent(MediaStore.INTENT_ACTION_MEDIA_PLAY_FROM_SEARCH).putExtras(extras)
        var target = appName
        if (appName.isNotBlank()) {
            val pkg = findPackage(ctx, appName) ?: return Res(false, "No app called \"$appName\" is installed")
            intent.setPackage(pkg)
        } else if (installed(ctx, STELLAR_DISC)) {
            intent.setPackage(STELLAR_DISC)
            target = "Stellar Disc"
        }
        return if (start(ctx, intent)) Res(true, "Asked ${target.ifBlank { "your music app" }} to play \"$query\"")
        else Res(false, if (appName.isBlank()) "No music app takes play requests; name one, e.g. Spotify"
                      else "$appName doesn't take play requests; I can open it with open_app instead")
    }

    /** Sends a song or video to Stellar Disc's Uplink downloader (it asks first unless the user turned that off). */
    fun uplinkDownload(ctx: Context, args: JSONObject): Res {
        if (!installed(ctx, STELLAR_DISC)) return Res(false, "Stellar Disc isn't installed on this phone")
        val url = args.optString("url", "").trim()
        val query = args.optString("query", "").trim()
        if (url.isEmpty() && query.isEmpty()) return Res(false, "Give a link or the song to search for")
        if (url.isNotEmpty() && !url.startsWith("http")) return Res(false, "The link must start with http")
        val intent = Intent("com.stellapp.disc.action.UPLINK").setPackage(STELLAR_DISC)
            .putExtra("url", url).putExtra("query", query)
            .putExtra("kind", args.optString("kind", ""))
            .putExtra("quality", args.optString("quality", ""))
            .putExtra("queue_when_done", args.optBoolean("queue_when_done", false))
        val what = if (url.isNotEmpty()) "the link" else "\"$query\" (top YouTube result)"
        return if (start(ctx, intent))
            Res(true, "Sent $what to Uplink in Stellar Disc. It downloads straight away if \"Let assistants start downloads\" is on; otherwise it's waiting for you to tap Download.")
        else Res(false, "Stellar Disc didn't take the request; update it to 3.4.0 or newer")
    }

    // ---- Sound ---------------------------------------------------------------------------------
    /** The setting an action is about to change, as it is now (for its Undo button); null if there's nothing to undo. */
    fun snapshot(ctx: Context, tool: String, args: JSONObject): JSONObject? = when (tool) {
        "set_media_volume" -> {
            val (stream, _) = volumeStream(args)
            val audio = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val max = audio.getStreamMaxVolume(stream).coerceAtLeast(1)
            JSONObject().put("percent", Math.round(100f * audio.getStreamVolume(stream) / max))
                .put("stream", args.optString("stream", "media"))
        }
        "set_brightness" -> {
            val cr = ctx.contentResolver
            val auto = Settings.System.getInt(cr, Settings.System.SCREEN_BRIGHTNESS_MODE, 0) ==
                Settings.System.SCREEN_BRIGHTNESS_MODE_AUTOMATIC
            JSONObject().put("auto", auto)
                .put("percent", Math.round(100f * Settings.System.getInt(cr, Settings.System.SCREEN_BRIGHTNESS, 128) / 255))
        }
        "set_sound_mode" -> {
            val audio = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            JSONObject()
                .put("ringer", when (audio.ringerMode) {
                    AudioManager.RINGER_MODE_SILENT -> "silent"
                    AudioManager.RINGER_MODE_VIBRATE -> "vibrate"
                    else -> "normal"
                })
                .put("dnd", nm.currentInterruptionFilter != NotificationManager.INTERRUPTION_FILTER_ALL)
        }
        else -> null
    }

    private fun volumeStream(args: JSONObject): Pair<Int, String> = when (args.optString("stream", "media").lowercase()) {
        "ring", "ringer", "ringtone" -> AudioManager.STREAM_RING to "Ring"
        "alarm", "alarms" -> AudioManager.STREAM_ALARM to "Alarm"
        "notification", "notifications" -> AudioManager.STREAM_NOTIFICATION to "Notification"
        "call", "voice" -> AudioManager.STREAM_VOICE_CALL to "Call"
        else -> AudioManager.STREAM_MUSIC to "Media"
    }

    fun setVolume(ctx: Context, args: JSONObject): Res {
        val percent = args.optInt("percent", -1)
        if (percent !in 0..100) return Res(false, "Volume must be 0-100")
        val (stream, name) = volumeStream(args)
        val audio = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val max = audio.getStreamMaxVolume(stream)
        try {
            audio.setStreamVolume(stream, Math.round(max * percent / 100f), AudioManager.FLAG_SHOW_UI)
        } catch (_: SecurityException) {   // ring/notification volume to 0 counts as Do Not Disturb
            return Res(false, NEED_DND)
        }
        return Res(true, "$name volume set to $percent%")
    }

    fun setSoundMode(ctx: Context, args: JSONObject): Res {
        val mode = args.optString("mode", "").lowercase().replace(' ', '_')
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val audio = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        return when (mode) {
            "dnd_on", "do_not_disturb", "dnd" -> {
                if (!hasDndAccess(ctx)) Res(false, NEED_DND)
                else { nm.setInterruptionFilter(NotificationManager.INTERRUPTION_FILTER_PRIORITY); Res(true, "Do Not Disturb on") }
            }
            "dnd_off" -> {
                if (!hasDndAccess(ctx)) Res(false, NEED_DND)
                else { nm.setInterruptionFilter(NotificationManager.INTERRUPTION_FILTER_ALL); Res(true, "Do Not Disturb off") }
            }
            "silent" -> {
                if (!hasDndAccess(ctx)) Res(false, NEED_DND)
                else { audio.ringerMode = AudioManager.RINGER_MODE_SILENT; Res(true, "Phone set to silent") }
            }
            "vibrate" -> try {
                audio.ringerMode = AudioManager.RINGER_MODE_VIBRATE; Res(true, "Phone set to vibrate")
            } catch (_: SecurityException) { Res(false, NEED_DND) }
            "normal", "ring", "sound" -> try {
                audio.ringerMode = AudioManager.RINGER_MODE_NORMAL; Res(true, "Sound on")
            } catch (_: SecurityException) { Res(false, NEED_DND) }
            else -> Res(false, "Mode must be normal, vibrate, silent, dnd_on or dnd_off")
        }
    }

    // ---- Screen --------------------------------------------------------------------------------
    fun setBrightness(ctx: Context, args: JSONObject): Res {
        if (!hasWriteSettings(ctx)) return Res(false, NEED_WRITE)
        val cr = ctx.contentResolver
        if (args.optBoolean("auto", false)) {
            Settings.System.putInt(cr, Settings.System.SCREEN_BRIGHTNESS_MODE, Settings.System.SCREEN_BRIGHTNESS_MODE_AUTOMATIC)
            return Res(true, "Automatic brightness on")
        }
        val percent = args.optInt("percent", -1)
        if (percent !in 0..100) return Res(false, "Brightness must be 0-100, or auto")
        Settings.System.putInt(cr, Settings.System.SCREEN_BRIGHTNESS_MODE, Settings.System.SCREEN_BRIGHTNESS_MODE_MANUAL)
        Settings.System.putInt(cr, Settings.System.SCREEN_BRIGHTNESS, maxOf(1, Math.round(255 * percent / 100f)))
        return Res(true, "Brightness set to $percent%")
    }

    /** Wi-Fi, Bluetooth and mobile data can't be switched by apps any more: this opens the right panel. */
    fun openSettings(ctx: Context, page: String): Res {
        val p = page.lowercase().trim()
        val action = when {
            p.startsWith("wifi") || p.startsWith("wi-fi") ->
                if (Build.VERSION.SDK_INT >= 29) Settings.Panel.ACTION_WIFI else Settings.ACTION_WIFI_SETTINGS
            p.startsWith("internet") || p.contains("data") || p.contains("mobile") ->
                if (Build.VERSION.SDK_INT >= 29) Settings.Panel.ACTION_INTERNET_CONNECTIVITY else Settings.ACTION_WIRELESS_SETTINGS
            p.startsWith("bluetooth") -> Settings.ACTION_BLUETOOTH_SETTINGS
            p.startsWith("volume") || p.startsWith("sound") ->
                if (Build.VERSION.SDK_INT >= 29) Settings.Panel.ACTION_VOLUME else Settings.ACTION_SOUND_SETTINGS
            p.startsWith("nfc") -> if (Build.VERSION.SDK_INT >= 29) Settings.Panel.ACTION_NFC else Settings.ACTION_NFC_SETTINGS
            p.startsWith("display") || p.startsWith("screen") -> Settings.ACTION_DISPLAY_SETTINGS
            p.startsWith("battery") -> Settings.ACTION_BATTERY_SAVER_SETTINGS
            p.startsWith("location") || p.startsWith("gps") -> Settings.ACTION_LOCATION_SOURCE_SETTINGS
            p.startsWith("airplane") || p.startsWith("flight") -> Settings.ACTION_AIRPLANE_MODE_SETTINGS
            p.startsWith("hotspot") || p.startsWith("tether") -> Settings.ACTION_WIRELESS_SETTINGS
            else -> Settings.ACTION_SETTINGS
        }
        return if (start(ctx, Intent(action))) Res(true, "Opened ${page.ifBlank { "settings" }} — tap the switch you want")
        else Res(false, "Couldn't open those settings")
    }

    // ---- Notifications -------------------------------------------------------------------------
    fun readNotifications(ctx: Context, args: JSONObject): Res {
        if (!hasNotificationAccess(ctx)) return Res(false, NEED_NOTIF)
        val svc = StellaNotificationListener.instance
            ?: return Res(false, "Notification access is on but Android hasn't connected it yet; try again in a moment")
        val appFilter = args.optString("app", "").trim()
        val pkgFilter = if (appFilter.isNotBlank()) findPackage(ctx, appFilter) else null
        val limit = args.optInt("limit", 10).coerceIn(1, 25)
        val list = try { svc.activeNotifications.toList() } catch (_: Exception) { emptyList() }
        val items = JSONArray()
        list.asSequence()
            .filter { it.packageName != ctx.packageName && !it.isOngoing }
            .filter { pkgFilter == null || it.packageName == pkgFilter }
            .sortedByDescending { it.postTime }
            .take(limit)
            .forEach { sbn ->
                val ex = sbn.notification.extras
                val title = ex.getCharSequence("android.title")?.toString().orEmpty()
                val text = (ex.getCharSequence("android.bigText") ?: ex.getCharSequence("android.text"))?.toString().orEmpty()
                if (title.isBlank() && text.isBlank()) return@forEach
                items.put(JSONObject().put("app", appLabel(ctx, sbn.packageName)).put("title", title)
                    .put("text", text.take(400))
                    .put("time", Instant.ofEpochMilli(sbn.postTime).atZone(ZoneId.systemDefault())
                        .format(DateTimeFormatter.ofPattern("EEE h:mm a"))))
            }
        val msg = if (items.length() == 0) "No notifications" + (if (appFilter.isNotBlank()) " from $appFilter" else "")
                  else "${items.length()} notification(s)"
        return Res(true, msg, JSONObject().put("notifications", items))
    }

    // ---- Contacts and calendar -----------------------------------------------------------------
    fun findContact(ctx: Context, args: JSONObject): Res {
        if (!has(ctx, Manifest.permission.READ_CONTACTS))
            return Res(false, "Stella needs Contacts permission: StellAI Settings → Phone access → Contacts & calendar")
        val name = args.optString("name", "").trim()
        if (name.isEmpty()) return Res(false, "Whose number?")
        val out = JSONArray()
        val uri = ContactsContract.CommonDataKinds.Phone.CONTENT_URI
        val cols = arrayOf(ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME, ContactsContract.CommonDataKinds.Phone.NUMBER,
            ContactsContract.CommonDataKinds.Phone.TYPE)
        ctx.contentResolver.query(uri, cols, "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} LIKE ?",
            arrayOf("%$name%"), "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} ASC")?.use { cur ->
            while (cur.moveToNext() && out.length() < 10) {
                val type = ContactsContract.CommonDataKinds.Phone.getTypeLabel(ctx.resources, cur.getInt(2), "").toString()
                out.put(JSONObject().put("name", cur.getString(0)).put("number", cur.getString(1)).put("type", type))
            }
        }
        return if (out.length() == 0) Res(true, "No contact matches \"$name\"", JSONObject().put("contacts", out))
        else Res(true, "Found ${out.length()} number(s) for \"$name\"", JSONObject().put("contacts", out))
    }

    fun upcomingEvents(ctx: Context, args: JSONObject): Res {
        if (!has(ctx, Manifest.permission.READ_CALENDAR))
            return Res(false, "Stella needs Calendar permission: StellAI Settings → Phone access → Contacts & calendar")
        val days = args.optInt("days", 7).coerceIn(1, 60)
        val now = System.currentTimeMillis()
        val uri = CalendarContract.Instances.CONTENT_URI.buildUpon().also {
            android.content.ContentUris.appendId(it, now)
            android.content.ContentUris.appendId(it, now + days * 86_400_000L)
        }.build()
        val cols = arrayOf(CalendarContract.Instances.TITLE, CalendarContract.Instances.BEGIN, CalendarContract.Instances.END,
            CalendarContract.Instances.EVENT_LOCATION, CalendarContract.Instances.ALL_DAY)
        val out = JSONArray()
        val fmt = DateTimeFormatter.ofPattern("EEE d MMM, h:mm a")
        ctx.contentResolver.query(uri, cols, null, null, "${CalendarContract.Instances.BEGIN} ASC")?.use { cur ->
            while (cur.moveToNext() && out.length() < 30) {
                val begin = Instant.ofEpochMilli(cur.getLong(1)).atZone(ZoneId.systemDefault())
                out.put(JSONObject().put("title", cur.getString(0).orEmpty())
                    .put("start", if (cur.getInt(4) == 1) begin.format(DateTimeFormatter.ofPattern("EEE d MMM")) + " (all day)"
                                  else begin.format(fmt))
                    .put("location", cur.getString(3).orEmpty()))
            }
        }
        return Res(true, if (out.length() == 0) "Nothing on the calendar in the next $days days"
                       else "${out.length()} event(s) in the next $days days", JSONObject().put("events", out))
    }

    // ---- Helpers -------------------------------------------------------------------------------
    /** The user's own music player (github.com/courier-iot/Stellar-Disc-Player). */
    private const val STELLAR_DISC = "com.stellapp.disc"

    private fun installed(ctx: Context, pkg: String): Boolean = try {
        ctx.packageManager.getPackageInfo(pkg, 0)
        true
    } catch (_: PackageManager.NameNotFoundException) {
        false
    }

    /** Package of an installed app by its name ("spotify" → com.spotify.music). */
    fun findPackage(ctx: Context, rawName: String): String? {
        val query = rawName.trim().lowercase()
        if (query.isEmpty()) return null
        val pm = ctx.packageManager
        val launcher = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        @Suppress("DEPRECATION")
        val apps = pm.queryIntentActivities(launcher, 0).map { it.loadLabel(pm).toString().lowercase() to it.activityInfo.packageName }
        return (apps.firstOrNull { it.first == query }
            ?: apps.firstOrNull { it.first.startsWith(query) }
            ?: apps.firstOrNull { it.first.contains(query) }
            ?: apps.firstOrNull { it.first.length >= 3 && query.contains(it.first) })?.second
    }

    private fun start(ctx: Context, intent: Intent): Boolean = try {
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        ctx.startActivity(intent)
        true
    } catch (_: Exception) {
        false
    }

    /** Android's own settings page for each kind of access. */
    fun accessIntent(ctx: Context, kind: String): Intent = when (kind) {
        "notifications" -> Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
        "dnd" -> Intent(Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS)
        "write" -> Intent(Settings.ACTION_MANAGE_WRITE_SETTINGS, Uri.parse("package:${ctx.packageName}"))
        else -> Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${ctx.packageName}"))
    }
}
