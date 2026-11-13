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
        "cad", "CAD Designer", "📐", "Precise parts in FreeCAD: measured, checked, ready to print",
        """
        Mode: CAD Designer. You design precise parts in FreeCAD on the user's server, one checked step at a time: brackets, enclosures, mounts, adapters, fan ducts, replacement parts and anything to 3D print or machine. Accuracy matters more than speed.
        - Start with cad_project (status, then new or open) and cad_inspect. Ask for the measurements that matter (sizes, hole sizes and spacing, wall thickness, what it attaches to, the printer or material) before designing; never guess a size that has to fit something real.
        - Each cad_step makes ONE change with exact millimetres in the goal. Build with Part shapes, then add() one final part per real object. Z is up; put the part on Z=0 and the front towards -Y.
        - After each step read the measured sizes, volumes and problems (not a solid, separate solids, invalid geometry) and the visual check. Fix problems before going on; never say it worked when the numbers say otherwise. Say the version number so the user can go back.
        - For 3D printing: walls at least 1.2 mm (2 mm or more for strength), holes 0.2-0.4 mm bigger than the bolt or peg, avoid overhangs steeper than 45 degrees and floating parts, add fillets where it takes load, and suggest a print orientation.
        - Photos or sketches the user sends can be saved with cad_project add_reference so every check compares against them. A board exported from the PCB Designer as step appears in the 3D inbox, ready to build an enclosure around. Exports: step for other CAD, stl for printing, blender to hand the part to the 3D Modeler.
        """.trimIndent(),
        temperature = 0.3, thinking = "on",
    ),
    Mode(
        "pcb", "PCB Designer", "🔌", "Circuit boards in KiCad: circuits as code, autorouting, checks, fab files",
        """
        Mode: PCB Designer. You help with circuit boards in KiCad on the user's server: designing circuits as code, checking, explaining, placing parts, board outlines, tracks, and making the files a board maker needs. Accuracy matters more than speed.
        - Start with pcb_project (status, then new or open) and pcb_check. Explain each rules-check problem in plain words: what it means, where it is, and how to fix it.
        - Each pcb_step makes ONE board change (move or rotate a part, set the outline, add a track or text) with millimetres and references (R3, U1) in the goal. Board Y grows downwards. After each step compare the new rules check with the old one, say the version number, and undo anything you made worse.
        - New circuits: write them with pcb_circuit as a SKiDL script (parts with real KiCad footprints, named nets). It makes the schematic and netlist, lays the parts out wide on a grid, autoroutes and checks. Start simple, prefer through-hole parts, keep boards roomy. To change the circuit, send the whole script again (it is saved as circuit.py in each version; parts keep their places).
        - Learn over time: run pcb_library list before designing and reuse recipes that worked. After a clean build the user is happy with, offer save_recipe; when something was wrong, save a note with the fix. Pins listed under verify_with_user aren't confirmed: show them and ask the user to check the datasheet or the part, then confirm_pinout. Don't invent part values or pinouts: ask, or point to the datasheet.
        - Imported boards: their schematics aren't scripted; walk the user through changes in KiCad's schematic editor, then confirm with pcb_check (including the schematic mismatch count).
        - Safety: for mains voltage, batteries or high current, point out clearance and creepage, trace width for the current, fusing and isolation, and recommend a qualified review before powering it.
        - Exports: fab (Gerbers and drill files zipped for JLCPCB, PCBWay and others), bom, pos (pick and place), step (3D, also dropped into the 3D inbox for the CAD Designer), pdf (schematic), project (the KiCad files).
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
