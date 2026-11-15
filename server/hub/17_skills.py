# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Skills: short how-to guides Stella loads when she needs them ---------------------------------------------------
# A small model with a good recipe beats a bigger one guessing. Only each skill's name and one line go in her
# instructions; she calls use_skill to read the whole guide before that kind of work. Built-in skills ship here; your
# own live as Markdown files in the skills folder next to the chat database (Settings → Skills, or ask Stella to save
# one). A skill of yours with a built-in's name replaces it.
SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{1,39}")
SKILL_MAX = 6000
BUILT_IN_SKILLS = {
    "cad-part": {
        "description": "Designing a part in FreeCAD (brackets, mounts, enclosures, plates) for 3D printing or CAD",
        "triggers": r"freecad|\bcad\b|bracket|enclosure|\bmount\b|baseplate|base plate|3d print|\bstl\b|\bstep file",
        "body": """1. Sizes first. If a size that changes the design is missing (overall size, hole size or spacing, wall
   thickness), ask for it in one short message and stop until the user answers. Otherwise pick sensible values and
   say which ones you assumed.
2. cad_project action=new with a short name for the part (it's also the workspace name).
3. Build in small cad_step calls, one feature each: the main body first, then pockets and cut-outs, then holes,
   then fillets and chamfers last (they fail most often, so they go on a finished shape).
4. After every step read the measured sizes in the result and compare them with the goal. If a size is wrong or the
   check reports broken geometry, fix it in the next step before adding anything else.
5. Finish with cad_inspect render=true and check the whole part against what the user asked for.
6. Export: stl for 3D printing, step for other CAD programs. Tell the user the file name and where it is.
Handy numbers: clearance holes M2 2.4, M3 3.2, M4 4.3, M5 5.3 mm. For 3D printing keep walls at least 1.2 mm
and add 0.2 mm to holes that must fit a part. Millimetres, Z up, the front faces -Y.""",
    },
    "blender-model": {
        "description": "Making or changing a 3D model in Blender (props, characters, avatar parts)",
        "triggers": r"blender|\b3d model|\bmesh\b|\bvrm\b|\bglb\b|sculpt|\brig",
        "body": """1. blender_inspect first (render=true if you haven't seen the model) so you use the real object names and sizes.
2. If the user gave pictures, keep them with blender_project action=add_reference; every check then compares
   against them.
3. One blender_step per change, with a goal that has numbers (metres, Z up, front faces -Y). Big shapes first,
   details later; name every object you make.
4. Read each step's measurements and the visual check. If they disagree with the goal, fix that before moving on.
   If a step fails twice the same way, try a different approach or ask the user.
5. blender_generate makes a rough mesh from photos; it's slow, so offer it before using it.
6. Export when the user is happy: glb for apps and the avatar, vrm for avatars, stl for printing.""",
    },
    "pcb-board": {
        "description": "Designing a circuit board in KiCad from a description (schematic, layout, Gerbers)",
        "triggers": r"kicad|\bpcb\b|circuit board|gerber|footprint",
        "body": """1. Agree the circuit first: the parts, power (voltage and current) and connectors. Ask if something that
   changes the board is unclear, and stop until the user answers.
2. pcb_library action=list: reuse saved recipes and confirmed pinouts instead of guessing.
3. pcb_circuit with the whole circuit as SKiDL code. Every part needs a real KiCad footprint (prefer through-hole);
   name the nets and give power nets a connector or a power flag. Start simple, then add parts.
4. Read the ERC and DRC results. Fix every error before anything else; explain any warning you leave.
5. pcb_step for small layout changes (moving a part, a track, the outline).
6. pcb_project action=export format=fab for the board maker (plus bom and pos for assembly).
7. When a sub-circuit built cleanly, offer to keep it with pcb_library action=save_recipe.""",
    },
    "research": {
        "description": "Finding current facts online (prices, versions, news, how-tos) and citing them",
        "triggers": r"\b(latest|current|price|cost|news|today|newest|release|compare)\b",
        "body": """1. web_search with a short, specific query (2-6 words). Search again with different words if the results miss.
2. fetch_page the two or three best results; snippets alone are often outdated or wrong.
3. Answer from what the pages say, with the links. If sources disagree, say so; if nothing answers it, say that
   rather than guessing.""",
    },
    "debug-code": {
        "description": "Finding and fixing a bug or an error message in code",
        "triggers": r"\bbug\b|\berror\b|exception|traceback|doesn'?t work|won'?t compile|crash",
        "body": """1. Work from the exact error message and the code. If either is missing, ask for it and stop.
2. Say what you think the cause is and why, in a sentence or two.
3. When you can, reproduce it with run_python before changing anything, then check that the fix works.
4. Make the smallest change that fixes the cause, and show the whole changed function.
5. Mention any other bug you noticed on the way, but don't fix things the user didn't ask about.""",
    },
    "github-fix": {
        "description": "Fixing a failed GitHub build or changing code in the user's repository",
        "triggers": r"github|\brepo\b|build fail|failed build|actions|workflow|pull request",
        "body": """1. github_builds to read the failed build's error, or github_issues for the problem.
2. github_read the files involved, the whole relevant part, before deciding on a fix.
3. Explain the fix in a few lines.
4. github_commit the full changed files to a new branch (never the main branch); the user approves it.
5. Offer github_pull_request, then suggest checking the build of that branch.""",
    },
}
SKILLS_STATE = {"dir": ""}


def _skills_dir():
    d = SKILLS_STATE["dir"] or os.path.join(MOVE["base"] or ".", "skills")
    os.makedirs(d, exist_ok=True)
    return d


def _read_skill_file(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    meta, body = {}, text
    m = re.match(r"^---\s*\n([\s\S]*?)\n---\s*\n?", text)
    if m:
        body = text[m.end():]
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if k.strip() and v.strip():
                meta[k.strip().lower()] = v.strip()
    return meta, body.strip()


def skills_list():
    """[{name, description, source, triggers, body}]: yours (they replace built-ins of the same name), then built-in."""
    out = {}
    for name, s in BUILT_IN_SKILLS.items():
        out[name] = {"name": name, "description": s["description"], "source": "built-in", "triggers": s["triggers"],
                     "body": s["body"]}
    try:
        files = sorted(os.listdir(_skills_dir()))
    except OSError:
        files = []
    for fn in files:
        if not fn.endswith(".md") or not SKILL_NAME.fullmatch(fn[:-3]):
            continue
        try:
            meta, body = _read_skill_file(os.path.join(_skills_dir(), fn))
        except (OSError, UnicodeDecodeError):
            continue
        name = fn[:-3]
        out[name] = {"name": name, "description": meta.get("description", "")[:160], "source": meta.get("source", "user"),
                     "triggers": meta.get("triggers", ""), "body": body[:SKILL_MAX],
                     "replaces_built_in": name in BUILT_IN_SKILLS}
    return sorted(out.values(), key=lambda s: (s["source"] == "built-in", s["name"]))


def skill_get(name):
    name = str(name or "").strip().lower().replace(" ", "-")
    return next((s for s in skills_list() if s["name"] == name), None)


def skill_save(data, source="user"):
    name = str(data.get("name") or "").strip().lower().replace(" ", "-")
    if not SKILL_NAME.fullmatch(name):
        raise ValueError("a skill name is 2-40 lowercase letters, digits and dashes, e.g. solder-smd")
    desc = " ".join(str(data.get("description") or "").split())[:160]
    body = str(data.get("body") or data.get("steps") or "").strip()
    if not desc or not body:
        raise ValueError("a skill needs a one-line description and its steps")
    if len(body) > SKILL_MAX:
        raise ValueError(f"keep a skill under {SKILL_MAX} characters")
    triggers = " ".join(str(data.get("triggers") or "").split())[:200]
    if triggers and not re.search(r"[|\\()\[\]]", triggers):   # plain words ("solder, smd") → a pattern
        triggers = r"\b(" + "|".join(re.escape(w) for w in re.split(r"\s*,\s*|\s+", triggers) if w) + ")"
    if triggers:
        try:
            re.compile(triggers)
        except re.error:
            triggers = ""
    head = f"---\ndescription: {desc}\nsource: {source}\n" + (f"triggers: {triggers}\n" if triggers else "") + "---\n"
    path = os.path.join(_skills_dir(), name + ".md")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(head + body + "\n")
    os.replace(path + ".tmp", path)
    return skill_get(name)


def skill_delete(name):
    """Deletes your skill (a built-in one you replaced comes back). False if there was nothing to delete."""
    name = str(name or "").strip().lower()
    if not SKILL_NAME.fullmatch(name):
        return False
    path = os.path.join(_skills_dir(), name + ".md")
    if not os.path.exists(path):
        return False
    os.remove(path)
    return True


def skills_section():
    """The few lines in Stella's instructions that tell her which skills exist."""
    skills = skills_list()
    if not skills:
        return ""
    return ("## Skills\nStep-by-step guides. Before starting that kind of task, call use_skill with its name and "
            "follow it:\n" + "\n".join(f"- {s['name']}: {s['description']}" for s in skills))


def skills_for(text):
    """Skills whose trigger words match this message (suggested to Stella in the message's context)."""
    hits = []
    for s in skills_list():
        try:
            if s.get("triggers") and re.search(s["triggers"], text or "", re.I):
                hits.append(s["name"])
        except re.error:
            continue
    return hits[:2]


def tool_use_skill(args):
    s = skill_get(args.get("name"))
    if not s:
        names = ", ".join(x["name"] for x in skills_list())
        raise ValueError(f"no skill called '{args.get('name')}'. Skills: {names}")
    learned = skill_lessons(s["name"])
    steps = s["body"] + ("\n\nLessons from past mistakes:\n" + "\n".join(f"- {t}" for t in learned) if learned else "")
    return {"skill": s["name"], "steps": steps}, f"Using the {s['name']} skill"


def tool_save_skill(args):
    if skill_get(args.get("name")) and skill_get(args.get("name"))["source"] == "built-in" and not args.get("replace"):
        raise ValueError("that's a built-in skill's name: pick another name, or set replace=true if the user wants "
                         "to replace it")
    s = skill_save(args, source="stella")
    return {"saved": s["name"], "note": "The user can edit or delete it in Settings → Skills."}, \
        f"Saved the {s['name']} skill"


SERVER_TOOLS["use_skill"] = (tool_use_skill, _fn(
    "use_skill", "Read a skill (a step-by-step guide from the Skills list) before doing that kind of task.",
    {"name": _p("The skill's name, e.g. cad-part")}, ["name"]))
SERVER_TOOLS["save_skill"] = (tool_save_skill, _fn(
    "save_skill",
    "Save a new skill: a short numbered how-to for a kind of task, so it goes better next time. Only when the user "
    "asks you to (e.g. 'save how we did this as a skill').",
    {"name": _p("lowercase-with-dashes, e.g. solder-smd"), "description": _p("One line: when to use it"),
     "steps": _p("Numbered steps, short and specific, with the numbers and tools that worked"),
     "triggers": _p("Optional words that suggest it, e.g. 'solder, smd, reflow'"),
     "replace": _p("true only if the user wants to replace a built-in skill of that name", "boolean")},
    ["name", "description", "steps"]))
TOOL_GROUPS["skills"] = ["save_skill"]   # use_skill is always offered in server replies when there are skills
TOOL_TRIGGERS["skills"] = re.compile(r"\bskills?\b", re.I)
