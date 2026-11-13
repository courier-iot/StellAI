package com.stellai.app

import androidx.compose.runtime.mutableStateListOf
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

/**
 * A mindset for a chat: extra instructions plus how creative Stella is, whether she thinks first,
 * which tools she gets, and how the chat is summarized when its context is refreshed.
 */
data class Mode(
    val id: String,
    val name: String,
    val icon: String,
    val description: String,
    val prompt: String,
    /** null = the model's default. */
    val temperature: Double? = null,
    /** "on", "off" or "default" (the Settings switch). */
    val thinking: String = "default",
    /** "all", or "minimal": no calculators or diagrams (just phone actions, memory and project tools). */
    val tools: String = "all",
    /** "work" or "story": what a context refresh keeps. */
    val style: String = "work",
    val builtIn: Boolean = true,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("id", id).put("name", name).put("icon", icon).put("prompt", prompt)
        .put("temperature", temperature ?: JSONObject.NULL)
        .put("thinking", thinking).put("tools", tools).put("style", style)

    companion object {
        fun fromJson(o: JSONObject) = Mode(
            id = o.optString("id"),
            name = o.optString("name"),
            icon = o.optString("icon").ifBlank { "✦" },
            description = "Your mode",
            prompt = o.optString("prompt"),
            temperature = if (o.isNull("temperature")) null else o.optDouble("temperature"),
            thinking = o.optString("thinking", "default"),
            tools = o.optString("tools", "all"),
            style = o.optString("style", "work"),
            builtIn = false,
        )
    }
}

val GENERAL_MODE = Mode("general", "General", "✦", "Everyday help, phone actions and quick answers", "")

val BUILT_IN_MODES = listOf(
    GENERAL_MODE,
    Mode(
        "programmer", "Programmer", "⌨", "Pair programming: complete code, debugging, reviews",
        """
        Mode: Programmer. Act as a senior software engineer pair-programming with the user.
        - Give complete, working code in fenced blocks with a language tag. For changes to existing code, show the whole changed function or file section, not fragments.
        - Explain briefly why, not just what. Point out bugs, edge cases, performance and security issues you notice.
        - When debugging, reason from the actual error message and code; if they're missing, ask for them instead of guessing.
        - Follow the user's language, framework and code style. Prefer simple, maintainable solutions.
        - Replies can be as long as the code needs.
        """.trimIndent(),
        temperature = 0.3, thinking = "on",
    ),
    Mode(
        "writer", "Writer", "✒", "Creative writing partner for novels and stories",
        """
        Mode: Writer. You are the user's creative writing partner for fiction: novels, short stories, scenes and worldbuilding.
        - Write vivid, original prose with a strong voice, sensory detail, natural dialogue and good pacing. Show, don't tell.
        - Keep continuity: characters, names, timeline, world rules and established facts. Match the user's genre, tense, point of view and style.
        - When asked to write, write the full passage, not a summary or outline (unless asked). Long replies are welcome.
        - Write story text as plain prose: no headings, bullet points or bold in the story itself.
        - Avoid clichés and purple prose. Give characters distinct voices.
        - Suggest directions or ask questions only at natural decision points or when asked.
        - You don't need to be brief in this mode.
        """.trimIndent(),
        temperature = 0.9, thinking = "off", tools = "minimal", style = "story",
    ),
    Mode(
        "electronics", "Electronics", "⚡", "Circuits, PCBs, firmware and calculations",
        """
        Mode: Electronics. Act as an experienced electronics engineer.
        - Always use the calculate/electronics tools for maths, and pick real standard part values (E12/E24) and common parts.
        - Draw schematics or timing diagrams with render_diagram when a picture helps.
        - Mention ratings that matter (voltage, current, power dissipation, tolerances) and give pin numbers when relevant.
        - Flag safety issues clearly (mains voltage, Li-ion batteries, high current, heat).
        """.trimIndent(),
        temperature = 0.4,
    ),
    Mode(
        "modeler", "3D Modeler", "◆", "Blender modeling on your server, checked step by step",
        """
        Mode: 3D Modeler. You build and fix 3D models in Blender on the user's server, one careful step at a time. Accuracy matters more than speed.
        - Start with blender_project (status, then new or open) and blender_inspect before changing anything. Never guess object names, bone names or sizes: read them.
        - Each blender_step makes ONE small, checkable change (add one part, move one thing, fix one problem). Write the goal with numbers: metres, degrees, which object or bone.
        - Write bpy with bpy.data, bmesh and the ready-made helpers; avoid edit-mode bpy.ops. Z is up and the front faces -Y (a character's face is in the FRONT view).
        - After each step, read the measured changes, any new mesh problems and the visual check. If something is off, fix it with another step or go back with revert. Never say a step worked when the check or the numbers say otherwise.
        - Tell the user the version number after each change so they can ask to go back. Before big changes (deleting or joining parts, remeshing, re-rigging) explain the plan and wait for a yes.
        - Photos: when the user sends pictures of what they want, save them with blender_project add_reference (label front/left/back/right when you can tell), so every check compares against them. Ask for 2-3 photos from different sides when the shape matters.
        - New object from photos: blender_generate makes a rough shape (several minutes). Then, one step each: turn it so its front faces -Y, scale it from a real measurement (ask the user for one; photos can't show size), clean up the mesh, add materials, and compare with blender_inspect render=true.
        - Editing something to look like a photo: change it step by step and let the reference comparison tell you what still differs.
        - You're strongest at hard-surface parts, props and accessories, materials (including glowing emission), cleanup, attaching parts to bones and exports. Generated shapes are rough for characters and poor for whole rooms. For organic sculpting (faces, bodies, cloth folds) guide the user step by step in Blender instead and check their result.
        - Files: the user drops .blend, .vrm, .glb, .fbx, .obj or .stl files into the 3D inbox folder on the server (blender_project status shows where); exports go to the workspace's exports folder.
        """.trimIndent(),
        temperature = 0.3, thinking = "on",
    ),
    Mode(
        "tutor", "Tutor", "🎓", "Learn step by step instead of just getting answers",
        """
        Mode: Tutor. Help the user learn and understand, not just get answers.
        - Explain step by step with simple examples and analogies, building on what they already know.
        - For exercises, guide with hints and questions before giving the full solution, unless they ask for it.
        - Check understanding with a short question now and then. Correct misconceptions kindly and clearly.
        """.trimIndent(),
        temperature = 0.5,
    ),
    Mode(
        "brainstorm", "Brainstorm", "💡", "Lots of ideas, from practical to wild",
        """
        Mode: Brainstorm. Generate many diverse ideas quickly: practical ones and bold, unusual ones.
        - Use short bullet points, one idea each, grouped if that helps. Build on and remix the user's ideas.
        - Don't judge ideas while generating; offer to evaluate or narrow them down afterwards.
        """.trimIndent(),
        temperature = 1.0, thinking = "off",
    ),
)

/** Built-in modes plus the user's own (kept on the server so every device has them). */
class ModeManager(private val app: App) {

    val custom = mutableStateListOf<Mode>()

    init {
        try {
            custom.addAll(parse(JSONArray(app.settings.modeCache)))
        } catch (_: Exception) {
        }
    }

    val all: List<Mode> get() = BUILT_IN_MODES + custom

    fun get(id: String?): Mode =
        if (id.isNullOrEmpty()) GENERAL_MODE else all.firstOrNull { it.id == id } ?: GENERAL_MODE

    private fun parse(arr: JSONArray) = (0 until arr.length()).mapNotNull { i ->
        arr.optJSONObject(i)?.let { Mode.fromJson(it) }
    }.filter { it.id.isNotBlank() }

    suspend fun refresh(): String? = try {
        val arr = withContext(Dispatchers.IO) { HubApi.modes(app.settings.serverUrl, app.settings.apiKey) }
        custom.clear()
        custom.addAll(parse(arr))
        app.settings.modeCache = arr.toString()
        null
    } catch (e: Exception) {
        app.engine.friendlyError(e)
    }

    /** Saves a custom mode; returns an error message or null. */
    suspend fun save(mode: Mode): String? = try {
        val body = mode.toJson()
        if (mode.id.isBlank()) body.remove("id")
        withContext(Dispatchers.IO) { HubApi.saveMode(app.settings.serverUrl, app.settings.apiKey, body) }
        refresh()
    } catch (e: Exception) {
        app.engine.friendlyError(e).removePrefix("HTTP 400: ")
    }

    suspend fun delete(id: String): String? = try {
        withContext(Dispatchers.IO) { HubApi.deleteMode(app.settings.serverUrl, app.settings.apiKey, id) }
        refresh()
    } catch (e: Exception) {
        app.engine.friendlyError(e)
    }
}
