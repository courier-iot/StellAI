package com.stellai.app

import android.Manifest
import android.app.AlarmManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

data class Reminder(val id: String, val text: String, val dueAt: Long, val repeat: String, val done: Boolean) {
    /** Identifies one occurrence (a repeating reminder gets a new one each time). */
    val key get() = "$id|$dueAt"

    companion object {
        fun fromJson(o: JSONObject) = Reminder(
            o.optString("id"), o.optString("text"), o.optLong("due_at"), o.optString("repeat", "none"), o.optBoolean("done"),
        )
    }
}

data class ListItem(val id: String, val text: String, val done: Boolean)

/**
 * Reminders live on the server and ring on every phone: each phone schedules exact alarms for them
 * and checks for changes every 15 minutes (and whenever the app syncs). Lists are shown from the server.
 */
class ReminderManager(private val app: App) {

    val items = mutableStateListOf<Reminder>()
    var lists by mutableStateOf<Map<String, List<ListItem>>>(emptyMap())
        private set
    var status by mutableStateOf("")

    init {
        items.addAll(cached(app))
    }

    private val url get() = app.settings.serverUrl
    private val key get() = app.settings.apiKey

    /** Fetches reminders, reschedules alarms. Returns false if the server couldn't be reached. */
    suspend fun refresh(): Boolean {
        val list = withContext(Dispatchers.IO) { fetchAndSchedule(app) } ?: return false.also {
            status = "Couldn't reach the server"
        }
        items.clear()
        items.addAll(list)
        status = ""
        return true
    }

    suspend fun refreshLists() {
        try {
            val obj = withContext(Dispatchers.IO) { HubApi.lists(url, key) }
            val out = linkedMapOf<String, List<ListItem>>()
            obj.keys().forEach { name ->
                val arr = obj.optJSONArray(name) ?: JSONArray()
                out[name] = (0 until arr.length()).mapNotNull { i ->
                    arr.optJSONObject(i)?.let { ListItem(it.optString("id"), it.optString("text"), it.optBoolean("done")) }
                }
            }
            lists = out
        } catch (e: Exception) {
            status = "Couldn't load lists: ${app.engine.friendlyError(e)}"
        }
    }

    private suspend fun attempt(block: () -> Unit) {
        try {
            withContext(Dispatchers.IO) { block() }
        } catch (e: Exception) {
            status = app.engine.friendlyError(e).removePrefix("HTTP 400: ")
        }
    }

    suspend fun add(text: String, dueAt: Long, repeat: String) {
        attempt { HubApi.addReminder(url, key, text, dueAt, repeat) }
        refresh()
    }

    suspend fun cancel(id: String) {
        attempt { HubApi.reminderAction(url, key, id, JSONObject().put("cancel", true)) }
        refresh()
    }

    suspend fun addItem(list: String, text: String) {
        attempt { HubApi.addToList(url, key, list, text) }
        refreshLists()
    }

    suspend fun setItem(id: String, done: Boolean) {
        attempt { HubApi.setListItem(url, key, id, done) }
        refreshLists()
    }

    suspend fun deleteItem(id: String) {
        attempt { HubApi.deleteListItem(url, key, id) }
        refreshLists()
    }

    suspend fun clearTicked(list: String) {
        attempt { HubApi.clearList(url, key, list) }
        refreshLists()
    }

    companion object {
        const val CHANNEL = "reminders"
        private const val PREFS = "reminders"

        fun cached(context: Context): List<Reminder> {
            val raw = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString("cache", "[]") ?: "[]"
            return try {
                val arr = JSONArray(raw)
                (0 until arr.length()).mapNotNull { arr.optJSONObject(it)?.let { o -> Reminder.fromJson(o) } }
            } catch (_: Exception) {
                emptyList()
            }
        }

        /** Blocking. Sends any queued Done/Snooze taps, fetches reminders and schedules alarms. Null if offline. */
        fun fetchAndSchedule(context: Context): List<Reminder>? {
            val app = App.instance
            if (app.settings.apiKey.isBlank()) return null
            flushPending(context)
            val arr = try {
                HubApi.reminders(app.settings.serverUrl, app.settings.apiKey)
            } catch (_: Exception) {
                return null
            }
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString("cache", arr.toString()).apply()
            val list = (0 until arr.length()).mapNotNull { arr.optJSONObject(it)?.let { o -> Reminder.fromJson(o) } }
            schedule(context, list)
            return list
        }

        private fun alarmIntent(context: Context, r: Reminder): PendingIntent {
            val i = Intent(context, ReminderReceiver::class.java).setAction(ReminderReceiver.FIRE)
                .putExtra("id", r.id).putExtra("text", r.text).putExtra("due", r.dueAt)
            return PendingIntent.getBroadcast(context, r.id.hashCode(), i,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        }

        /** Sets an exact alarm for each upcoming reminder and cancels alarms/notifications that are no longer due. */
        fun schedule(context: Context, list: List<Reminder>) {
            val am = context.getSystemService(AlarmManager::class.java) ?: return
            val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val now = System.currentTimeMillis()
            val notified = prefs.getStringSet("notified", emptySet()) ?: emptySet()
            // Missed while the phone was off: still ring if it was due in the last 12 hours
            val active = list.filter { !it.done && it.dueAt > now - 12 * 3600_000L && it.key !in notified }
            val activeIds = active.map { it.id }.toSet()
            for (old in prefs.getStringSet("scheduled", emptySet()) ?: emptySet()) {
                val id = old.substringBefore('|')
                if (id !in activeIds) am.cancel(alarmIntent(context, Reminder(id, "", 0, "none", false)))
            }
            val nm = NotificationManagerCompat.from(context)
            list.filter { it.done }.forEach { nm.cancel(notificationId(it.id)) }   // dismissed on another device
            for (r in active) setAlarm(context, r, maxOf(r.dueAt, now + 1500))
            prefs.edit()
                .putStringSet("scheduled", active.map { it.key }.toSet())
                .putStringSet("notified", notified.filter { k -> list.any { it.key == k } }.toSet())
                .apply()
        }

        /** One exact alarm (falls back to an inexact one if exact alarms aren't allowed). */
        fun setAlarm(context: Context, r: Reminder, at: Long) {
            val am = context.getSystemService(AlarmManager::class.java) ?: return
            val exact = Build.VERSION.SDK_INT < 31 || am.canScheduleExactAlarms()
            try {
                if (exact) am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, alarmIntent(context, r))
                else am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, alarmIntent(context, r))
            } catch (_: SecurityException) {
                am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, alarmIntent(context, r))
            }
        }

        fun notificationId(id: String) = 0x5000 + (id.hashCode() and 0xFFFF)

        fun markNotified(context: Context, key: String) {
            val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            prefs.edit().putStringSet("notified", (prefs.getStringSet("notified", emptySet()) ?: emptySet()) + key).apply()
        }

        fun wasNotified(context: Context, key: String) =
            key in (context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getStringSet("notified", emptySet()) ?: emptySet())

        /** Done/Snooze taps that couldn't reach the server yet. */
        fun queueAction(context: Context, id: String, action: JSONObject) {
            val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val arr = try { JSONArray(prefs.getString("pending", "[]")) } catch (_: Exception) { JSONArray() }
            arr.put(JSONObject().put("id", id).put("action", action))
            prefs.edit().putString("pending", arr.toString()).apply()
        }

        private fun flushPending(context: Context) {
            val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val arr = try { JSONArray(prefs.getString("pending", "[]")) } catch (_: Exception) { JSONArray() }
            if (arr.length() == 0) return
            val s = App.instance.settings
            val left = JSONArray()
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                try {
                    HubApi.reminderAction(s.serverUrl, s.apiKey, o.optString("id"), o.optJSONObject("action") ?: JSONObject())
                } catch (_: Exception) {
                    left.put(o)
                }
            }
            prefs.edit().putString("pending", left.toString()).apply()
        }

        fun createChannel(context: Context) {
            val nm = context.getSystemService(NotificationManager::class.java) ?: return
            if (nm.getNotificationChannel(CHANNEL) == null) {
                nm.createNotificationChannel(NotificationChannel(CHANNEL, "Reminders", NotificationManager.IMPORTANCE_HIGH).apply {
                    description = "Reminders you set with Stella"
                    enableVibration(true)
                })
            }
        }

        /** Background check every 15 minutes, so reminders set from another device reach this phone. */
        fun startBackgroundSync(context: Context) {
            val req = PeriodicWorkRequestBuilder<ReminderWorker>(15, TimeUnit.MINUTES)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork("reminders", ExistingPeriodicWorkPolicy.KEEP, req)
        }
    }
}

class ReminderWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        if (ReminderManager.fetchAndSchedule(applicationContext) != null) Result.success() else Result.retry()
    }
}

/** Rings reminders and handles the Done / Snooze buttons. Also reschedules after a reboot. */
class ReminderReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val pending = goAsync()
        CoroutineScope(SupervisorJob() + Dispatchers.IO).launch {
            try {
                handle(context, intent)
            } finally {
                pending.finish()
            }
        }
    }

    private fun handle(context: Context, intent: Intent) {
        val id = intent.getStringExtra("id").orEmpty()
        val s = App.instance.settings
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED -> {
                ReminderManager.schedule(context, ReminderManager.cached(context))
                ReminderManager.startBackgroundSync(context)
            }
            FIRE -> {
                val text = intent.getStringExtra("text").orEmpty()
                val due = intent.getLongExtra("due", 0)
                val key = "$id|$due"
                if (ReminderManager.wasNotified(context, key)) return
                // Quick check that it wasn't done or moved on another device (ring anyway if offline)
                val fresh = kotlinx.coroutines.runBlocking {
                    withTimeoutOrNull(6000) { ReminderManager.fetchAndSchedule(context) }
                }
                val current = fresh?.firstOrNull { it.id == id }
                if (fresh != null && (current == null || current.done || current.dueAt > due + 60_000)) return
                ReminderManager.markNotified(context, key)
                notify(context, id, current?.text ?: text)
            }
            DONE, SNOOZE -> {
                NotificationManagerCompat.from(context).cancel(ReminderManager.notificationId(id))
                val action = if (intent.action == DONE) JSONObject().put("done", true) else JSONObject().put("snooze_minutes", 10)
                try {
                    HubApi.reminderAction(s.serverUrl, s.apiKey, id, action)
                } catch (_: Exception) {
                    ReminderManager.queueAction(context, id, action)
                    if (intent.action == SNOOZE) {   // still ring in 10 minutes even offline
                        val text = intent.getStringExtra("text").orEmpty()
                        val at = System.currentTimeMillis() + 600_000
                        ReminderManager.setAlarm(context, Reminder(id, text, at, "none", false), at)
                    }
                }
                ReminderManager.fetchAndSchedule(context)
            }
        }
    }

    private fun notify(context: Context, id: String, text: String) {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) return
        ReminderManager.createChannel(context)
        fun action(act: String, code: Int) = PendingIntent.getBroadcast(
            context, id.hashCode() + code,
            Intent(context, ReminderReceiver::class.java).setAction(act).putExtra("id", id).putExtra("text", text),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val open = PendingIntent.getActivity(
            context, 7, Intent(context, MainActivity::class.java).putExtra(MainActivity.EXTRA_SCREEN, "tasks")
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val n = NotificationCompat.Builder(context, ReminderManager.CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_listen)
            .setContentTitle("Reminder")
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_REMINDER)
            .setDefaults(NotificationCompat.DEFAULT_ALL)
            .setContentIntent(open)
            .setAutoCancel(true)
            .addAction(0, "Done", action(DONE, 1))
            .addAction(0, "Snooze 10 min", action(SNOOZE, 2))
            .build()
        try {
            NotificationManagerCompat.from(context).notify(ReminderManager.notificationId(id), n)
        } catch (_: SecurityException) {
        }
    }

    companion object {
        const val FIRE = "com.stellai.app.REMINDER_FIRE"
        const val DONE = "com.stellai.app.REMINDER_DONE"
        const val SNOOZE = "com.stellai.app.REMINDER_SNOOZE"
    }
}
