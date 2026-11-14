package com.stellai.app

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.text.format.DateUtils
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.ime
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.union
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material.icons.filled.Search
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Folder
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.outlined.Psychology
import androidx.compose.material.icons.outlined.Check
import androidx.compose.material.icons.outlined.Mic
import androidx.compose.material.icons.outlined.PhotoCamera
import androidx.compose.material.icons.outlined.Edit
import androidx.compose.material.icons.outlined.WarningAmber
import androidx.compose.material.icons.outlined.Dns
import androidx.compose.material.icons.outlined.LocalLibrary
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.NavigationDrawerItem
import androidx.compose.material3.NavigationDrawerItemDefaults
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.rememberDrawerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch
import androidx.compose.foundation.layout.ColumnScope

// Small pieces of the chat screen: the status dot, the listening ring, the empty-chat screen, Auto chips.

/** Small glowing dot: cyan when Stella is active or listening, dim when idle. */
@Composable
internal fun StatusDot(online: Boolean) {
    val color = if (online) Space.Accent else Space.Muted
    Box(
        Modifier
            .size(8.dp)
            .background(color, CircleShape)
            .border(3.dp, color.copy(alpha = 0.25f), CircleShape)
    )
}

/** Expanding ring behind the mic button while listening. Only exists while listening. */
@Composable
internal fun PulseRing() {
    val pulse = rememberInfiniteTransition(label = "pulse")
    val scale by pulse.animateFloat(
        initialValue = 0.85f,
        targetValue = 1.3f,
        animationSpec = infiniteRepeatable(tween(900), RepeatMode.Reverse),
        label = "scale",
    )
    Box(
        Modifier
            .size(48.dp)
            .scale(scale)
            .background(Space.Accent.copy(alpha = 0.22f), CircleShape)
    )
}

@Composable
internal fun EmptyState(modifier: Modifier, app: App, name: String, project: Project?, mode: Mode) {
    Box(modifier.fillMaxSize().padding(32.dp), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            if (mode.id != GENERAL_MODE.id) {
                IconText(LineIcons.forMode(mode.icon), "${mode.name.uppercase()} MODE",
                    style = HudLabel.copy(fontSize = 13.sp, letterSpacing = 3.sp), color = Space.Glow)
                Spacer(Modifier.size(6.dp))
                Text(mode.description, textAlign = TextAlign.Center, color = Space.Muted, style = MaterialTheme.typography.bodySmall)
                Spacer(Modifier.size(24.dp))
            }
            if (project != null) {
                IconText(Icons.Outlined.Folder, project.name.uppercase(), style = HudLabel.copy(fontSize = 13.sp, letterSpacing = 3.sp),
                    color = accentFor(project.color).main)
                Spacer(Modifier.size(6.dp))
                Text(
                    "New chat in this project. $name knows its instructions, " +
                        "${project.notes.size} note${if (project.notes.size == 1) "" else "s"} and " +
                        "${project.files.size} file${if (project.files.size == 1) "" else "s"}.",
                    textAlign = TextAlign.Center,
                    color = Space.Muted,
                    style = MaterialTheme.typography.bodySmall,
                )
                Spacer(Modifier.size(28.dp))
            }
            Icon(LineIcons.Sparkle, contentDescription = null, tint = Space.Accent, modifier = Modifier.size(52.dp))
            Spacer(Modifier.size(8.dp))
            Text(
                "${name.uppercase()} ONLINE",
                style = HudLabel.copy(fontSize = 18.sp, letterSpacing = 5.sp),
                color = Space.Text,
            )
            Spacer(Modifier.size(16.dp))
            val wake = if (app.wakeListening) "Say “${app.settings.wakeWordLabel}, set an alarm for 6:45”\nor tap the microphone\n\n" else "Tap the microphone and speak, or type:\n"
            Text(
                wake + "“Start a 10 minute timer”\n“Open Spotify”\n“Turn on the flashlight”",
                textAlign = TextAlign.Center,
                color = Space.Muted,
            )
        }
    }
}

/** Modes Auto can switch between (the chip's "Redo in …" choices). */
internal val AUTO_CHOICES = listOf("general", "programmer", "electronics", "cad", "pcb", "modeler", "writer", "tutor", "brainstorm")
