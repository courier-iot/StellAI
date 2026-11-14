package com.stellai.app

import android.content.Context
import android.content.Intent
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.net.Uri
import android.os.BatteryManager
import android.provider.AlarmClock
import android.provider.CalendarContract
import org.json.JSONArray
import org.json.JSONObject
import java.time.LocalDateTime
import java.time.ZoneId

/**
 * Phone actions the AI on the laptop can ask for.
 * Anything that sends or calls (texts, phone calls, calendar events) only opens
 * the right screen pre-filled, so you always press the final button yourself.
 *
 * To add a tool: add a definition in [definitions] and a branch in [execute].
 */
object PhoneTools {

    /** summary = the chat line ("✓ …" or "✗ …"); result = what the model is told; button = Cancel/Undo, if any. */
    data class Outcome(val summary: String, val result: String, val button: ActionButton? = null)

    fun definitions(): JSONArray = JSONArray().apply {
        put(tool("set_alarm", "Wake-up alarm in the clock app.",
            props(
                "hour" to prop("integer", "0-23"),
                "minute" to prop("integer", "0-59"),
                "label" to prop("string", "optional"),
            ), "hour", "minute"))
        put(tool("set_timer", "Start a countdown timer.",
            props(
                "seconds" to prop("integer", "length"),
                "label" to prop("string", "optional"),
            ), "seconds"))
        put(tool("open_app", "Open an installed app by name.",
            props("app_name" to prop("string", "e.g. Spotify")),
            "app_name"))
        put(tool("open_url", "Open a web page in the browser.",
            props("url" to prop("string", "https://…")), "url"))
        put(tool("web_search", "Search the web on the phone.",
            props("query" to prop("string", "What to search for")), "query"))
        put(tool("navigate_to", "Directions in maps.",
            props("destination" to prop("string", "place or address")), "destination"))
        put(tool("draft_text_message",
            "Draft a text in the messaging app (user sends it).",
            props(
                "message" to prop("string", "text"),
                "phone_number" to prop("string", "optional"),
            ), "message"))
        put(tool("dial_number", "Open the dialer with a number (user calls).",
            props("phone_number" to prop("string", "number")), "phone_number"))
        put(tool("create_calendar_event",
            "Draft a calendar event (user saves it).",
            props(
                "title" to prop("string", "title"),
                "start" to prop("string", "YYYY-MM-DD HH:MM"),
                "duration_minutes" to prop("integer", "default 60"),
                "location" to prop("string", "optional"),
            ), "title", "start"))
        put(tool("set_flashlight", "Turn the flashlight on or off.",
            props("on" to prop("boolean", "on/off")), "on"))
        put(tool("set_media_volume", "Set a volume on the phone (media by default).",
            props("percent" to prop("integer", "0-100"),
                "stream" to prop("string", "media (default), ring, alarm, notification or call")), "percent"))
        put(tool("get_battery_status", "Battery level and charging state.", props()))
        put(tool("cancel_alarm",
            "Turn off an alarm in the clock app (by time, label, the next one, or all). Apps can't delete or list alarms.",
            props("time" to prop("string", "HH:MM 24-hour, optional"), "label" to prop("string", "optional"),
                "which" to prop("string", "'next' (default) or 'all'"))))
        put(tool("alarm_info", "When the next alarm rings.", props()))
        put(tool("open_clock", "Show the alarms or timers list in the clock app.",
            props("view" to prop("string", "alarms or timers")), "view"))
        put(tool("cancel_timer", "Stop the running or ringing timer.", props()))
        put(tool("media_control", "Control the music or video that's playing.",
            props("action" to prop("string", "play, pause, toggle, next, previous, restart, stop, forward, rewind"),
                "app" to prop("string", "optional player, e.g. Spotify")), "action"))
        put(tool("now_playing", "What song or video is playing now.", props()))
        put(tool("play_music", "Play a song, artist, album or playlist by name in a music app.",
            props("query" to prop("string", "e.g. 'Bohemian Rhapsody Queen'"),
                "kind" to prop("string", "song, artist, album, playlist or any"),
                "app" to prop("string", "optional, e.g. Spotify; default Stellar Disc if installed")), "query"))
        put(tool("uplink_download",
            "Download a song or video into Stellar Disc with its Uplink downloader. Give a url (e.g. one found with web_search) or a query to take the top YouTube result.",
            props("query" to prop("string", "song and artist, e.g. 'Midnight City M83'"),
                "url" to prop("string", "optional link instead of a search"),
                "kind" to prop("string", "music (default) or video"),
                "quality" to prop("string", "best, balanced or saver (default: the user's Autopilot choice)"),
                "queue_when_done" to prop("boolean", "add it to the play queue when it's downloaded"))))
        put(tool("set_sound_mode", "Ringer and Do Not Disturb.",
            props("mode" to prop("string", "normal, vibrate, silent, dnd_on or dnd_off")), "mode"))
        put(tool("set_brightness", "Screen brightness.",
            props("percent" to prop("integer", "0-100"), "auto" to prop("boolean", "true = automatic"))))
        put(tool("open_settings", "Open a settings panel (apps can't switch Wi-Fi, Bluetooth or mobile data themselves).",
            props("page" to prop("string", "wifi, bluetooth, internet, volume, nfc, display, battery, location, airplane, hotspot")),
            "page"))
        put(tool("read_notifications", "Read the phone's current notifications (messages, emails, apps).",
            props("app" to prop("string", "optional, e.g. WhatsApp"), "limit" to prop("integer", "default 10"))))
        put(tool("find_contact", "Look up a contact's phone numbers by name (then draft_text_message or dial_number).",
            props("name" to prop("string", "name or part of it")), "name"))
        put(tool("upcoming_events", "Read the calendar's upcoming events.",
            props("days" to prop("integer", "how many days ahead, default 7"))))
    }

    fun displayName(name: String): String = when (name) {
        "set_alarm" -> "Setting alarm"
        "set_timer" -> "Starting timer"
        "open_app" -> "Opening app"
        "open_url" -> "Opening page"
        "web_search" -> "Searching"
        "navigate_to" -> "Opening maps"
        "draft_text_message" -> "Drafting text"
        "dial_number" -> "Opening dialer"
        "create_calendar_event" -> "Creating event"
        "set_flashlight" -> "Flashlight"
        "set_media_volume" -> "Setting volume"
        "get_battery_status" -> "Checking battery"
        "cancel_alarm" -> "Turning off alarm"
        "alarm_info" -> "Checking alarms"
        "open_clock" -> "Opening clock"
        "cancel_timer" -> "Stopping timer"
        "media_control" -> "Controlling music"
        "now_playing" -> "Checking what's playing"
        "play_music" -> "Playing music"
        "uplink_download" -> "Sending to Uplink"
        "set_sound_mode" -> "Changing sound mode"
        "set_brightness" -> "Setting brightness"
        "open_settings" -> "Opening settings"
        "read_notifications" -> "Reading notifications"
        "find_contact" -> "Looking up contact"
        "upcoming_events" -> "Reading calendar"
        else -> name
    }

    /** Must be called on the main thread. */
    fun execute(ctx: Context, name: String, argsJson: String): Outcome {
        val args = try {
            JSONObject(argsJson.ifBlank { "{}" })
        } catch (e: Exception) {
            JSONObject()
        }
        val before = try {
            PhoneExtras.snapshot(ctx, name, args)   // the setting as it was, so it can be undone
        } catch (_: Exception) {
            null
        }
        val out = perform(ctx, name, args)
        return if (out.summary.startsWith("✓")) out.copy(button = ActionButtons.forAction(name, args, before)) else out
    }

    private fun perform(ctx: Context, name: String, args: JSONObject): Outcome {
        return try {
            when (name) {
                "set_alarm" -> {
                    val hour = args.optInt("hour", -1)
                    val minute = args.optInt("minute", 0)
                    if (hour !in 0..23 || minute !in 0..59) return fail("Invalid time")
                    val label = args.optString("label", "")
                    val intent = Intent(AlarmClock.ACTION_SET_ALARM)
                        .putExtra(AlarmClock.EXTRA_HOUR, hour)
                        .putExtra(AlarmClock.EXTRA_MINUTES, minute)
                        .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
                    if (label.isNotBlank()) intent.putExtra(AlarmClock.EXTRA_MESSAGE, label)
                    val time = "%d:%02d".format(hour, minute)
                    launch(ctx, intent, "Alarm set for $time", "No clock app found")
                }
                "set_timer" -> {
                    val seconds = args.optInt("seconds", 0)
                    if (seconds <= 0) return fail("Invalid timer length")
                    val label = args.optString("label", "")
                    val intent = Intent(AlarmClock.ACTION_SET_TIMER)
                        .putExtra(AlarmClock.EXTRA_LENGTH, seconds)
                        .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
                    if (label.isNotBlank()) intent.putExtra(AlarmClock.EXTRA_MESSAGE, label)
                    launch(ctx, intent, "Timer started: ${formatDuration(seconds)}", "No clock app found")
                }
                "open_app" -> openApp(ctx, args.optString("app_name", ""))
                "open_url" -> {
                    var url = args.optString("url", "").trim()
                    if (url.isEmpty()) return fail("No URL given")
                    if (!url.startsWith("http://") && !url.startsWith("https://")) url = "https://$url"
                    launch(ctx, Intent(Intent.ACTION_VIEW, Uri.parse(url)), "Opened $url", "No browser found")
                }
                "web_search" -> {
                    val q = args.optString("query", "").trim()
                    if (q.isEmpty()) return fail("No search query")
                    val uri = Uri.parse("https://duckduckgo.com/?q=" + Uri.encode(q))
                    launch(ctx, Intent(Intent.ACTION_VIEW, uri), "Searched for \"$q\"", "No browser found")
                }
                "navigate_to" -> {
                    val dest = args.optString("destination", "").trim()
                    if (dest.isEmpty()) return fail("No destination")
                    val uri = Uri.parse("geo:0,0?q=" + Uri.encode(dest))
                    launch(ctx, Intent(Intent.ACTION_VIEW, uri), "Opened maps to $dest", "No maps app found")
                }
                "draft_text_message" -> {
                    val msg = args.optString("message", "")
                    val number = args.optString("phone_number", "").trim()
                    val intent = Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:" + Uri.encode(number)))
                        .putExtra("sms_body", msg)
                    launch(ctx, intent, "Text drafted (waiting for you to send)", "No messaging app found")
                }
                "dial_number" -> {
                    val number = args.optString("phone_number", "").trim()
                    if (number.isEmpty()) return fail("No phone number")
                    val intent = Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + Uri.encode(number)))
                    launch(ctx, intent, "Dialer opened with $number", "No dialer found")
                }
                "create_calendar_event" -> createEvent(ctx, args)
                "set_flashlight" -> setFlashlight(ctx, args.optBoolean("on", true))
                "set_media_volume" -> extra(PhoneExtras.setVolume(ctx, args))
                "get_battery_status" -> {
                    val bm = ctx.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
                    val level = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
                    val charging = bm.isCharging
                    ok("Battery $level%" + if (charging) ", charging" else ", not charging")
                }
                "cancel_alarm" -> extra(PhoneExtras.cancelAlarm(ctx, args))
                "alarm_info" -> extra(PhoneExtras.alarmInfo(ctx))
                "open_clock" -> extra(PhoneExtras.openClock(ctx, args.optString("view", "alarms")))
                "cancel_timer" -> extra(PhoneExtras.cancelTimer(ctx))
                "media_control" -> extra(PhoneExtras.mediaControl(ctx, args))
                "now_playing" -> extra(PhoneExtras.nowPlaying(ctx))
                "play_music" -> extra(PhoneExtras.playMusic(ctx, args))
                "uplink_download" -> extra(PhoneExtras.uplinkDownload(ctx, args))
                "set_sound_mode" -> extra(PhoneExtras.setSoundMode(ctx, args))
                "set_brightness" -> extra(PhoneExtras.setBrightness(ctx, args))
                "open_settings" -> extra(PhoneExtras.openSettings(ctx, args.optString("page", "")))
                "read_notifications" -> extra(PhoneExtras.readNotifications(ctx, args))
                "find_contact" -> extra(PhoneExtras.findContact(ctx, args))
                "upcoming_events" -> extra(PhoneExtras.upcomingEvents(ctx, args))
                else -> fail("Unknown tool: $name")
            }
        } catch (e: Exception) {
            fail(e.message ?: e.javaClass.simpleName)
        }
    }

    // ---- Tool implementations -------------------------------------------------

    private fun openApp(ctx: Context, rawName: String): Outcome {
        val query = rawName.trim().lowercase()
        if (query.isEmpty()) return fail("No app name")
        val pm = ctx.packageManager
        val launcherIntent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        @Suppress("DEPRECATION")
        val apps = pm.queryIntentActivities(launcherIntent, 0)
            .map { it.loadLabel(pm).toString() to it.activityInfo.packageName }

        val match = apps.firstOrNull { it.first.lowercase() == query }
            ?: apps.firstOrNull { it.first.lowercase().startsWith(query) }
            ?: apps.firstOrNull { it.first.lowercase().contains(query) }
            ?: apps.firstOrNull { it.first.length >= 3 && query.contains(it.first.lowercase()) }
            ?: return fail("No app called \"$rawName\" is installed")

        val intent = pm.getLaunchIntentForPackage(match.second)
            ?: return fail("Couldn't open ${match.first}")
        return launch(ctx, intent, "Opened ${match.first}", "Couldn't open ${match.first}")
    }

    private fun createEvent(ctx: Context, args: JSONObject): Outcome {
        val title = args.optString("title", "").ifBlank { "Event" }
        val startText = args.optString("start", "").trim().replace(' ', 'T')
        val start = try {
            LocalDateTime.parse(startText)
        } catch (e: Exception) {
            return fail("Start time must look like 2026-10-29 15:00")
        }
        val minutes = args.optInt("duration_minutes", 60).coerceAtLeast(1)
        val zone = ZoneId.systemDefault()
        val begin = start.atZone(zone).toInstant().toEpochMilli()
        val end = start.plusMinutes(minutes.toLong()).atZone(zone).toInstant().toEpochMilli()
        val intent = Intent(Intent.ACTION_INSERT)
            .setData(CalendarContract.Events.CONTENT_URI)
            .putExtra(CalendarContract.EXTRA_EVENT_BEGIN_TIME, begin)
            .putExtra(CalendarContract.EXTRA_EVENT_END_TIME, end)
            .putExtra(CalendarContract.Events.TITLE, title)
        val location = args.optString("location", "")
        if (location.isNotBlank()) intent.putExtra(CalendarContract.Events.EVENT_LOCATION, location)
        return launch(ctx, intent, "Calendar opened with \"$title\" (waiting for you to save)", "No calendar app found")
    }

    private fun setFlashlight(ctx: Context, on: Boolean): Outcome {
        val cm = ctx.getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val id = cm.cameraIdList.firstOrNull {
            cm.getCameraCharacteristics(it).get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
        } ?: return fail("This phone has no flashlight")
        cm.setTorchMode(id, on)
        return ok(if (on) "Flashlight on" else "Flashlight off")
    }

    // ---- Helpers ----------------------------------------------------------------

    private fun launch(ctx: Context, intent: Intent, success: String, failure: String): Outcome =
        try {
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            ctx.startActivity(intent)
            ok(success)
        } catch (e: Exception) {
            fail(failure)
        }

    /** A PhoneExtras answer as a chat line plus the model's result (with any data it found). */
    private fun extra(r: PhoneExtras.Res): Outcome {
        val result = JSONObject().put("ok", r.ok).put(if (r.ok) "result" else "error", r.msg)
        r.data?.let { d -> d.keys().forEach { k -> result.put(k, d.get(k)) } }
        return Outcome((if (r.ok) "✓ " else "✗ ") + r.msg, result.toString())
    }

    private fun ok(msg: String) =
        Outcome("✓ $msg", JSONObject().put("ok", true).put("result", msg).toString())

    private fun fail(msg: String) =
        Outcome("✗ $msg", JSONObject().put("ok", false).put("error", msg).toString())

    private fun formatDuration(seconds: Int): String {
        val h = seconds / 3600
        val m = (seconds % 3600) / 60
        val s = seconds % 60
        return listOfNotNull(
            if (h > 0) "${h}h" else null,
            if (m > 0) "${m}m" else null,
            if (s > 0) "${s}s" else null,
        ).joinToString(" ")
    }

    private fun prop(type: String, description: String) =
        JSONObject().put("type", type).put("description", description)

    private fun props(vararg pairs: Pair<String, JSONObject>) =
        JSONObject().apply { pairs.forEach { (k, v) -> put(k, v) } }

    private fun tool(name: String, description: String, properties: JSONObject, vararg required: String) =
        JSONObject()
            .put("type", "function")
            .put("function", JSONObject()
                .put("name", name)
                .put("description", description)
                .put("parameters", JSONObject()
                    .put("type", "object")
                    .put("properties", properties)
                    .put("required", JSONArray(required.toList()))))
}
