// StellAI avatar: an animated space orb (no libraries) or a 3D VRM character (three.js + three-vrm).
// Used by the web chat's avatar screen (/avatar). States: idle, listening, thinking, talking.
// setLevel(0..1) is the loudness of Stella's voice right now (moves the mouth / pulses the orb).
// Also: createWakeWord() — openWakeWord in the browser (ONNX Runtime Web), same models as the phone app.

const COLORS = { idle: [92, 225, 230], listening: [77, 232, 160], thinking: [155, 123, 255], talking: [120, 235, 255] };
const lerp = (a, b, t) => a + (b - a) * t;
const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
const smoothstep = x => { x = clamp(x); return x * x * (3 - 2 * x); };

/** Slow devices (Raspberry Pi and similar): fewer pixels and frames. */
export function looksLikeSlowDevice() {
  const ua = navigator.userAgent || "", cores = navigator.hardwareConcurrency || 4;
  return /aarch64|armv7|armv8|Raspbian|CrOS armv/i.test(ua) && !/Android|iPhone|iPad/i.test(ua) || cores <= 2;
}

export function createAvatar(host, opts = {}) {
  let impl = null, style = "", state = "idle", level = 0, destroyed = false;
  const o = { camera: "upper", height: 172.7, light: false, gestures: true, animations: [], key: "", ...opts };
  const onError = o.onError || (() => {});

  let ver = "";
  async function setStyle(next, modelUrl, version = "") {
    if (next === style && impl && (next !== "vrm" || version === ver)) return;
    ver = version;
    if (impl) impl.destroy();
    impl = null; style = next; host.innerHTML = "";
    if (next === "vrm") {
      try {
        impl = await createVRM(host, modelUrl, o);
        if (destroyed || style !== "vrm") { impl.destroy(); return; }
      } catch (e) {
        onError(e);
        style = "orb"; host.innerHTML = "";
        impl = createOrb(host, o);
      }
    } else {
      impl = createOrb(host, o);
    }
    impl.setState(state); impl.setLevel(level);
  }
  const call = (fn, ...a) => impl && impl[fn] && impl[fn](...a);
  return {
    setStyle,
    get style() { return style; },
    setState(s) { state = s; call("setState", s); },
    setLevel(x) { level = x; call("setLevel", x); },
    setVisemes(v) { call("setVisemes", v); },
    setCamera(c) { o.camera = c; call("setCamera", c); },
    setHeight(cm) { o.height = cm; call("setHeight", cm); },
    setLight(on) { o.light = !!on; call("setLight", o.light); },
    setGestures(on) { o.gestures = !!on; },
    setAnimations(list) { o.animations = list || []; call("setAnimations", o.animations); },
    play(what) { return call("play", what); },
    trigger(tag) { call("trigger", tag); },
    react(text) { call("react", text); },
    fps() { return impl && impl.fps ? impl.fps() : 0; },
    destroy() { destroyed = true; if (impl) impl.destroy(); impl = null; host.innerHTML = ""; },
  };
}

// ------------------------------------------------------------------ the orb
function createOrb(host, o) {
  const canvas = document.createElement("canvas");
  canvas.style.cssText = "width:100%;height:100%;display:block";
  host.appendChild(canvas);
  const ctx = canvas.getContext("2d");
  let state = "idle", level = 0, smooth = 0, raf = 0, w = 0, h = 0, dpr = 1, last = performance.now(), t = 0, fps = 60, acc = 0;
  let color = COLORS.idle.slice(), spin = 0, ripple = 0;
  const stars = Array.from({ length: 140 }, () => ({ x: Math.random(), y: Math.random(), r: Math.random() * 1.3 + 0.2, s: Math.random() * 0.6 + 0.2, p: Math.random() * 6 }));
  const sparks = Array.from({ length: 36 }, (_, i) => ({ a: (i / 36) * Math.PI * 2, ring: i % 3 }));
  const ro = new ResizeObserver(resize); ro.observe(host);
  function resize() {
    dpr = o.light ? 1 : Math.min(2, window.devicePixelRatio || 1);
    w = host.clientWidth; h = host.clientHeight;
    canvas.width = Math.max(1, w * dpr); canvas.height = Math.max(1, h * dpr);
  }
  resize();
  function frame(now) {
    raf = requestAnimationFrame(frame);
    const step = (now - last) / 1000;
    if (o.light && step < 1 / 31) return;          // light mode: 30 frames a second
    const dt = Math.min(0.05, step); last = now; t += dt;
    fps = lerp(fps, 1 / Math.max(step, 1e-3), 0.05);
    smooth = lerp(smooth, level, level > smooth ? 0.5 : 0.15);
    const target = COLORS[state] || COLORS.idle;
    for (let i = 0; i < 3; i++) color[i] = lerp(color[i], target[i], 0.06);
    const speed = state === "thinking" ? 2.4 : state === "listening" ? 0.9 : state === "talking" ? 0.7 : 0.35;
    spin += dt * speed;
    ripple = state === "listening" ? (ripple + dt * 0.55) % 1 : 0;
    const c = `${color[0] | 0},${color[1] | 0},${color[2] | 0}`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const nStars = o.light ? 60 : stars.length;
    for (let i = 0; i < nStars; i++) {
      const s = stars[i], tw = 0.5 + 0.5 * Math.sin(t * s.s * 2 + s.p);
      ctx.fillStyle = `rgba(200,215,255,${0.15 + tw * 0.5})`;
      ctx.beginPath(); ctx.arc(((s.x + t * 0.004 * s.s) % 1) * w, s.y * h, s.r, 0, 7); ctx.fill();
    }
    const cx = w / 2, cy = h / 2, R = Math.min(w, h) * 0.17;
    const r = R * (1 + 0.035 * Math.sin(t * 1.6)) * (1 + smooth * 0.22);
    let g = ctx.createRadialGradient(cx, cy, r * 0.2, cx, cy, r * 3.2);
    g.addColorStop(0, `rgba(${c},0.35)`); g.addColorStop(0.4, `rgba(${c},0.10)`); g.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, r * 3.2, 0, 7); ctx.fill();
    if (state === "listening") {
      for (let k = 0; k < 3; k++) {
        const p = (ripple + k / 3) % 1;
        ctx.strokeStyle = `rgba(${c},${(1 - p) * 0.55})`; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(cx, cy, r * (1.1 + p * 1.6), 0, 7); ctx.stroke();
      }
    }
    for (let k = 0; k < 3; k++) {
      const tilt = [0.32, -0.55, 0.9][k], rx = r * (1.55 + k * 0.32), ry = rx * (0.28 + k * 0.08);
      ctx.save(); ctx.translate(cx, cy); ctx.rotate(tilt + spin * (k % 2 ? -0.15 : 0.12));
      ctx.strokeStyle = `rgba(${c},${0.18 + (state === "thinking" ? 0.15 : 0)})`; ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.ellipse(0, 0, rx, ry, 0, 0, 7); ctx.stroke();
      for (const s of sparks) {
        if (s.ring !== k) continue;
        const a = s.a + spin * (1 + k * 0.35) * (k % 2 ? -1 : 1), front = Math.sin(a) > 0;
        ctx.fillStyle = `rgba(${c},${front ? 0.95 : 0.35})`;
        ctx.beginPath(); ctx.arc(Math.cos(a) * rx, Math.sin(a) * ry, (front ? 2.4 : 1.4) * (state === "thinking" ? 1.3 : 1), 0, 7); ctx.fill();
      }
      ctx.restore();
    }
    if (smooth > 0.02 || state === "talking") {
      const n = o.light ? 48 : 72;
      ctx.strokeStyle = `rgba(${c},0.85)`; ctx.lineWidth = 2; ctx.beginPath();
      for (let i = 0; i <= n; i++) {
        const a = (i / n) * Math.PI * 2, wob = Math.sin(a * 6 + t * 9) * 0.5 + Math.sin(a * 11 - t * 13) * 0.5;
        const rr = r * (1.18 + smooth * 0.35 * (0.6 + 0.4 * wob));
        const x = cx + Math.cos(a) * rr, y = cy + Math.sin(a) * rr;
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      }
      ctx.stroke();
    }
    g = ctx.createRadialGradient(cx - r * 0.3, cy - r * 0.35, r * 0.05, cx, cy, r);
    g.addColorStop(0, "rgba(255,255,255,0.95)"); g.addColorStop(0.35, `rgba(${c},0.95)`); g.addColorStop(1, `rgba(${c},0.25)`);
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7); ctx.fill();
    if (state === "thinking") {
      for (let i = 0; i < 14; i++) {
        const a = spin * 2.2 - i * 0.09;
        ctx.fillStyle = `rgba(255,255,255,${0.8 * (1 - i / 14)})`;
        ctx.beginPath(); ctx.arc(cx + Math.cos(a) * r * 1.25, cy + Math.sin(a) * r * 1.25, 2.6 * (1 - i / 16), 0, 7); ctx.fill();
      }
    }
  }
  raf = requestAnimationFrame(frame);
  return {
    setState(s) { state = s; },
    setLevel(x) { level = clamp(x); },
    setLight() { resize(); },
    fps: () => fps,
    destroy() { cancelAnimationFrame(raf); ro.disconnect(); canvas.remove(); },
  };
}

// ------------------------------------------------------------------ the 3D character
// Mixamo bone names -> VRM humanoid bones (for .fbx animations from mixamo.com)
const MIXAMO = { Hips: "hips", Spine: "spine", Spine1: "chest", Spine2: "upperChest", Neck: "neck", Head: "head",
  LeftShoulder: "leftShoulder", LeftArm: "leftUpperArm", LeftForeArm: "leftLowerArm", LeftHand: "leftHand",
  RightShoulder: "rightShoulder", RightArm: "rightUpperArm", RightForeArm: "rightLowerArm", RightHand: "rightHand",
  LeftUpLeg: "leftUpperLeg", LeftLeg: "leftLowerLeg", LeftFoot: "leftFoot", LeftToeBase: "leftToes",
  RightUpLeg: "rightUpperLeg", RightLeg: "rightLowerLeg", RightFoot: "rightFoot", RightToeBase: "rightToes" };
for (const side of ["Left", "Right"]) {
  const s = side.toLowerCase();
  MIXAMO[side + "HandThumb1"] = s + "ThumbMetacarpal"; MIXAMO[side + "HandThumb2"] = s + "ThumbProximal"; MIXAMO[side + "HandThumb3"] = s + "ThumbDistal";
  for (const f of ["Index", "Middle", "Ring", "Pinky"]) {
    const v = f === "Pinky" ? "Little" : f;
    MIXAMO[side + "Hand" + f + "1"] = s + v + "Proximal"; MIXAMO[side + "Hand" + f + "2"] = s + v + "Intermediate"; MIXAMO[side + "Hand" + f + "3"] = s + v + "Distal";
  }
}
const BODY = ["hips", "spine", "chest", "upperChest", "neck", "head", "leftShoulder", "rightShoulder", "leftUpperArm", "rightUpperArm",
  "leftLowerArm", "rightLowerArm", "leftHand", "rightHand", "leftUpperLeg", "rightUpperLeg", "leftLowerLeg", "rightLowerLeg",
  "leftFoot", "rightFoot", "leftToes", "rightToes"];
const FINGERS = []; for (const s of ["left", "right"]) for (const f of ["Thumb", "Index", "Middle", "Ring", "Little"])
  for (const j of f === "Thumb" ? ["Metacarpal", "Proximal", "Distal"] : ["Proximal", "Intermediate", "Distal"]) FINGERS.push(s + f + j);

// Built-in gestures: offsets (radians) added to the relaxed pose, shaped by an envelope over `len` seconds.
// p = 0..1 progress, e = envelope 0..1, A = arm sign for this character. Tags say when each can play.
const GESTURES = {
  "Look around": { len: 4.5, tags: ["idle"], fn: (p, e) => ({ neck: [0, Math.sin(p * Math.PI * 2) * 0.35 * e, 0], head: [0, Math.sin(p * Math.PI * 2) * 0.25 * e, 0], spine: [0, Math.sin(p * Math.PI * 2) * 0.08 * e, 0], look: Math.sin(p * Math.PI * 2) * 1.6 * e }) },
  "Head tilt": { len: 3, tags: ["idle", "happy"], fn: (p, e) => ({ neck: [0, 0, 0.16 * e], head: [-0.05 * e, 0.05 * e, 0.1 * e], happy: 0.45 * e }) },
  "Weight shift": { len: 4, tags: ["idle"], fn: (p, e) => ({ hips: [0, 0.06 * e, Math.sin(p * Math.PI) * 0.05 * e], spine: [0, -0.04 * e, -Math.sin(p * Math.PI) * 0.05 * e], neck: [0, 0, -Math.sin(p * Math.PI) * 0.03 * e] }) },
  "Shoulder roll": { len: 2.6, tags: ["idle"], fn: (p, e, A) => { const s = Math.sin(p * Math.PI * 2) * e;
    return { leftShoulder: [0, 0, -0.12 * s * A], rightShoulder: [0, 0, 0.12 * s * A], chest: [-0.05 * e, 0, 0], neck: [0.06 * e, 0, 0] }; } },
  "Stretch": { len: 5, tags: ["quiet"], fn: (p, e, A) => { const up = smoothstep(clamp(p * 2.5)) * smoothstep(clamp((1 - p) * 2.5));
    return { leftUpperArm: [0, 0, 2.6 * up * A], rightUpperArm: [0, 0, -2.6 * up * A], leftLowerArm: [0, 0, 0.3 * up * A], rightLowerArm: [0, 0, -0.3 * up * A],
      spine: [-0.12 * up, 0, 0], chest: [-0.08 * up, 0, 0], head: [-0.15 * up, 0, 0], blink: up * 0.85, relaxed: up * 0.6 }; } },
  "Wave": { len: 3.2, tags: ["greeting"], fn: (p, e, A) => { const wv = Math.sin(p * Math.PI * 7) * 0.35 * e;
    return { rightUpperArm: [0, -0.3 * e * A, -1.5 * e * A], rightLowerArm: [0, 0, (-1.35 + wv) * e * A], rightHand: [0, 0, 0.1 * e],
      head: [0, -0.06 * e, -0.06 * e], happy: 0.7 * e }; } },
  // Blowing a kiss: right hand comes up to her lips (pucker, eyes closed), then sweeps out toward you
  // with an open hand and a wink and a smile. Uses arm IK so the hand really reaches her mouth on any character.
  "Blow a kiss": { len: 3.4, tags: ["kiss"], fn: (p, e) => {
    const ramp = (a, b) => smoothstep((p - a) / (b - a));
    const toLips = ramp(0.0, 0.28), out = ramp(0.48, 0.66), back = ramp(0.8, 1.0);
    const reach = toLips * (1 - back) * e;
    const pucker = toLips * (1 - out);
    return {
      ik: [{ side: "right", w: reach, target: out < 1e-3 ? { at: "lips" } : { at: "lips", to: "front", mix: out },
             pole: [0.3, -0.45, 0.05] }],
      straighten: { right: reach },
      neck: [0.04 * pucker - 0.03 * out, -0.06 * pucker, -0.06 * pucker + 0.05 * out],
      head: [0.05 * pucker - 0.06 * out, 0, -0.04 * pucker],
      spine: [0.02 * pucker - 0.02 * out, -0.04 * reach, 0],
      ou: 0.85 * pucker * e, blink: 0.75 * pucker * e,
      blinkLeft: out * (1 - ramp(0.72, 0.84)) * e,             // a quick wink as the kiss flies
      happy: (0.25 * pucker + 0.75 * out * (1 - back)) * e,
    }; } },
  // Hip-sway dance: both hands behind her head (elbows up and out), hips swaying right-to-left with a little
  // bounce on each step, upper body leaning the other way (an S-curve), head tilting with the beat, open smile.
  // Hair swings on its own (spring bones). Feet stay roughly planted.
  "Hip sway": { len: 9.6, tags: ["dance", "idle"], fn: (p, e) => {
    const ramp = (a, b) => smoothstep((p - a) / (b - a));
    const arms = ramp(0, 0.06) * (1 - ramp(0.94, 1)) * e;      // hands go up first, come down last
    const sw = Math.sin(p * Math.PI * 12) * ramp(0.04, 0.1) * (1 - ramp(0.9, 0.97)) * e;   // 6 sways, starting to her right
    const bounce = Math.abs(Math.sin(p * Math.PI * 12));      // up on each step
    const shift = -sw;                                        // + = toward her left
    const tilt = 0.14 * shift, twist = 0.12 * shift;
    const legL = 0.8, slide = Math.asin(clamp(0.05 * shift / legL, -0.2, 0.2));
    const bentL = Math.max(0, -shift), bentR = Math.max(0, shift);        // the leg without the weight bends
    return {
      ik: [{ side: "left", w: arms, target: { at: "behindHead" }, pole: [0.42, 0.3, 0.02] },
           { side: "right", w: arms, target: { at: "behindHead" }, pole: [0.42, 0.3, 0.02] }],
      hips: [0, twist, tilt], hipsX: 0.05 * shift, hipsY: 0.012 * bounce * e - 0.015 * e,
      leftUpperLeg: [-0.08 * bentL - 0.04 * e, 0, -tilt - slide], rightUpperLeg: [-0.08 * bentR - 0.04 * e, 0, -tilt - slide],
      leftLowerLeg: [0.22 * bentL + 0.08 * e, 0, 0], rightLowerLeg: [0.22 * bentR + 0.08 * e, 0, 0],
      leftFoot: [-0.12 * bentL, 0, 0], rightFoot: [-0.12 * bentR, 0, 0],
      spine: [-0.04 * e, -0.6 * twist, -0.75 * tilt], chest: [-0.03 * e, -0.3 * twist, -0.35 * tilt],
      neck: [0, 0.25 * twist, -0.2 * tilt], head: [0.03 * bounce * e, 0, 0.5 * tilt],
      happy: 0.3 * e, relaxed: 0.25 * e, aa: 0.35 * e,
    }; } },
  // Happy bounce: eyes closed in a big smile, hands tucked behind her back, shoulders up, bouncing on the beat
  // (knees dip, then she springs up with her head tipping back), head rocking side to side with each bounce.
  "Bounce": { len: 3.6, tags: ["happy", "greeting", "idle"], fn: (p, e, A) => {
    const beats = 7, ph = p * beats;                         // ~2 bounces a second
    const up = Math.pow(Math.abs(Math.sin(Math.PI * ph)), 0.7) * e, dip = (1 - Math.pow(Math.abs(Math.sin(Math.PI * ph)), 0.7)) * e;
    const side = Math.sin(Math.PI * ph) * e;                 // changes sign every bounce: rock left, then right
    return {
      leftUpperArm: [0, 0.45 * e * A, 0.1 * e * A], rightUpperArm: [0, -0.45 * e * A, -0.1 * e * A],     // arms swung back
      leftLowerArm: [0, 0.35 * e * A, 0], rightLowerArm: [0, -0.35 * e * A, 0],                          // hands meet behind
      leftShoulder: [0, 0, 0.1 * e * A], rightShoulder: [0, 0, -0.1 * e * A],                            // shoulders up
      leftUpperLeg: [-0.22 * dip, 0, 0], rightUpperLeg: [-0.22 * dip, 0, 0],                             // knees dip
      leftLowerLeg: [0.42 * dip, 0, 0], rightLowerLeg: [0.42 * dip, 0, 0],
      leftFoot: [-0.2 * dip, 0, 0], rightFoot: [-0.2 * dip, 0, 0],
      hipsY: (0.012 * up - 0.03 * dip),
      spine: [-0.05 * up, 0, 0.02 * side], chest: [-0.04 * up, 0, 0],
      neck: [0, 0.04 * side, 0.09 * side], head: [-0.1 * up, 0, 0.05 * side],
      happy: 0.95 * e, aa: 0.3 * up,
    }; } },
};

async function createVRM(host, url, o) {
  const THREE = await import("three");
  const { GLTFLoader } = await import("three/addons/loaders/GLTFLoader.js");
  const { VRMLoaderPlugin, VRMUtils } = await import("@pixiv/three-vrm");

  let light = !!o.light;
  const renderer = new THREE.WebGLRenderer({ antialias: !light, alpha: true, powerPreference: light ? "low-power" : "default" });
  const pixelRatio = () => light ? Math.min(1, window.devicePixelRatio || 1) * 0.85 : Math.min(2, window.devicePixelRatio || 1);
  renderer.setPixelRatio(pixelRatio());
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.domElement.style.cssText = "width:100%;height:100%;display:block";
  host.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  const cam = new THREE.PerspectiveCamera(28, 1, 0.05, 60);
  scene.add(new THREE.HemisphereLight(0xbfd8ff, 0x1a1030, 1.6));
  const key = new THREE.DirectionalLight(0xffffff, 2.2); key.position.set(1, 2, 3); scene.add(key);
  const rim = new THREE.DirectionalLight(0x5ce1e6, 1.4); rim.position.set(-2, 1.5, -2); scene.add(rim);

  const loader = new GLTFLoader();
  loader.register(parser => new VRMLoaderPlugin(parser));
  const gltf = await new Promise((res, rej) => loader.load(url, res, undefined, e => rej(new Error("couldn't load the character: " + (e && e.message || e)))));
  const vrm = gltf.userData.vrm;
  if (!vrm) throw new Error("that file isn't a VRM character");
  VRMUtils.removeUnnecessaryVertices && VRMUtils.removeUnnecessaryVertices(gltf.scene);
  (VRMUtils.combineSkeletons || VRMUtils.removeUnnecessaryJoints || (() => {}))(gltf.scene);
  VRMUtils.rotateVRM0 && VRMUtils.rotateVRM0(vrm);
  vrm.scene.traverse(obj => { obj.frustumCulled = false; });
  scene.add(vrm.scene);

  const bone = n => vrm.humanoid && vrm.humanoid.getNormalizedBoneNode(n);
  const raw = n => vrm.humanoid && (vrm.humanoid.getRawBoneNode ? vrm.humanoid.getRawBoneNode(n) : bone(n));
  const ex = vrm.expressionManager;
  const setEx = (n, v) => { try { ex && ex.setValue(n, clamp(v)); } catch {} };
  const lookTarget = new THREE.Object3D(); scene.add(lookTarget);
  if (vrm.lookAt) { vrm.lookAt.target = lookTarget; if ("autoUpdate" in vrm.lookAt) vrm.lookAt.autoUpdate = true; }
  const bones = {}; for (const n of BODY.concat(FINGERS)) { const b = bone(n); if (b) bones[n] = b; }
  const hipsRest = bones.hips ? bones.hips.position.clone() : new THREE.Vector3();

  // ---- size: scale the character to the chosen height (default 5'8" = 172.7 cm)
  const box = new THREE.Box3().setFromObject(vrm.scene);
  const nativeHeight = Math.max(0.3, box.max.y - box.min.y);
  const headPos = new THREE.Vector3();
  function measureHead() { vrm.scene.updateMatrixWorld(true); (raw("head") || vrm.scene).getWorldPosition(headPos); }
  function setHeight(cm) {
    const s = clamp((+cm || 172.7) / 100, 1.2, 2.2) / nativeHeight;
    vrm.scene.scale.setScalar(s);
    measureHead(); setCamera(cameraMode, true);
  }

  // ---- relaxed pose: arms down instead of the T-pose. The sign is checked (models differ), then fixed.
  let A = 1;
  const P = (n, x, y, z) => { const b = bones[n]; if (b) { b.rotation.x += x; b.rotation.y += y; b.rotation.z += z; } };
  function resetPose() {
    for (const n in bones) bones[n].quaternion.identity();
    if (bones.hips) bones.hips.position.copy(hipsRest);
  }
  function relaxed() {
    P("leftUpperArm", 0, 0, -1.22 * A); P("rightUpperArm", 0, 0, 1.22 * A);
    P("leftLowerArm", 0, -0.15, -0.12 * A); P("rightLowerArm", 0, 0.15, 0.12 * A);
    for (const s of ["left", "right"]) for (const f of ["Index", "Middle", "Ring", "Little"]) {   // soft, slightly curled fingers
      const sg = s === "left" ? -1 : 1; P(s + f + "Proximal", 0, 0, 0.18 * sg * A); P(s + f + "Intermediate", 0, 0, 0.22 * sg * A);
    }
  }
  // Arm IK (for gestures where a hand has to reach a spot, like her lips): rest directions of each arm bone
  const restDir = {}, V3 = THREE.Vector3, Q = THREE.Quaternion;
  resetPose(); vrm.update(0); vrm.scene.updateMatrixWorld(true);
  for (const sd of ["left", "right"]) {
    const U = bones[sd + "UpperArm"], L = bones[sd + "LowerArm"], H = bones[sd + "Hand"];
    if (!U || !L || !H) continue;
    const pU = U.getWorldPosition(new V3()), pL = L.getWorldPosition(new V3()), pH = H.getWorldPosition(new V3());
    restDir[sd + "UpperArm"] = pL.clone().sub(pU).normalize().applyQuaternion(U.parent.getWorldQuaternion(new Q()).invert());
    restDir[sd + "LowerArm"] = pH.clone().sub(pL).normalize().applyQuaternion(U.getWorldQuaternion(new Q()).invert());
  }
  const hipsW = bones.hips ? bones.hips.getWorldPosition(new V3()) : new V3();
  const rightSign = bones.rightUpperArm ? Math.sign(bones.rightUpperArm.getWorldPosition(new V3()).x - hipsW.x) || -1 : -1;
  function reach(sd, T, pole, w) {     // two-bone IK: shoulder -> elbow -> wrist to target T (world), elbow toward pole
    const U = bones[sd + "UpperArm"], L = bones[sd + "LowerArm"], H = bones[sd + "Hand"];
    if (!U || !L || !H || !restDir[sd + "UpperArm"] || w <= 0) return;
    vrm.scene.updateMatrixWorld(true);
    const S = U.getWorldPosition(new V3()), E0 = L.getWorldPosition(new V3()), H0 = H.getWorldPosition(new V3());
    const L1 = S.distanceTo(E0), L2 = E0.distanceTo(H0);
    const dir = T.clone().sub(S); const d = clamp(dir.length(), Math.abs(L1 - L2) + 1e-3, L1 + L2 - 1e-3); dir.normalize();
    const a = (L1 * L1 - L2 * L2 + d * d) / (2 * d), h = Math.sqrt(Math.max(0, L1 * L1 - a * a));
    const pp = pole.clone().sub(S); const perp = pp.sub(dir.clone().multiplyScalar(pp.dot(dir))).normalize();
    const E = S.clone().add(dir.clone().multiplyScalar(a)).add(perp.multiplyScalar(h));
    const Hn = S.clone().add(dir.clone().multiplyScalar(d));
    const u = E.clone().sub(S).normalize().applyQuaternion(U.parent.getWorldQuaternion(new Q()).invert());
    U.quaternion.slerp(new Q().setFromUnitVectors(restDir[sd + "UpperArm"], u), w);
    U.updateMatrixWorld(true);
    const f = Hn.clone().sub(E).normalize().applyQuaternion(U.getWorldQuaternion(new Q()).invert());
    L.quaternion.slerp(new Q().setFromUnitVectors(restDir[sd + "LowerArm"], f), w);
    L.updateMatrixWorld(true);
  }
  function ikTargets(spec, sd) {       // named spots, in metres for a 170 cm person, scaled to her height
    const k = (o.height || 172.7) / 170, sx = sd === "right" ? rightSign : -rightSign;
    const head = (bones.head || vrm.scene).getWorldPosition(new V3());
    const sh = bones[sd + "UpperArm"].getWorldPosition(new V3());
    const spot = n => n === "lips" ? head.clone().add(new V3(sx * 0.03 * k, -0.06 * k, 0.13 * k))
      : n === "front" ? sh.clone().add(new V3(sx * 0.1 * k, 0.12 * k, 0.48 * k))
      : n === "behindHead" ? head.clone().add(new V3(sx * 0.07 * k, 0.11 * k, -0.07 * k)) : head.clone();
    const T = spot(spec.at);
    if (spec.to) T.lerp(spot(spec.to), spec.mix || 0);
    return T;
  }

  resetPose(); relaxed(); vrm.update(0); vrm.scene.updateMatrixWorld(true);
  const pa = new THREE.Vector3(), pb = new THREE.Vector3();
  if (raw("leftHand") && raw("leftUpperArm")) {
    raw("leftHand").getWorldPosition(pa); raw("leftUpperArm").getWorldPosition(pb);
    if (pa.y > pb.y - 0.05) A = -1;
  }

  // ---- camera framing
  let cameraMode = o.camera || "upper";
  const camGoal = new THREE.Vector3(), camAim = new THREE.Vector3(), aimNow = new THREE.Vector3();
  function setCamera(mode, jump) {
    cameraMode = mode;
    const y = headPos.y, hgt = y / 0.93;
    if (mode === "face") { camAim.set(0, y + 0.02, 0); camGoal.set(0, y + 0.04, 0.75); }
    else if (mode === "full") { camAim.set(0, hgt * 0.52, 0); camGoal.set(0, hgt * 0.58, hgt * 2.5); }
    else { camAim.set(0, y - 0.18, 0); camGoal.set(0, y - 0.08, 1.75); }
    if (jump) { cam.position.copy(camGoal); aimNow.copy(camAim); }
  }
  setHeight(o.height);

  function resize() {
    const w = host.clientWidth || 1, h = host.clientHeight || 1;
    renderer.setSize(w, h, false); cam.aspect = w / h; cam.updateProjectionMatrix();
  }
  const ro = new ResizeObserver(resize); ro.observe(host); resize();

  // ---- animation clips (uploaded .vrma / Mixamo .fbx)
  const mixer = new THREE.AnimationMixer(vrm.scene);
  const clips = new Map();      // id -> {name, when, clip}
  let anim = null;              // {kind:"clip"|"gesture", ...}
  async function loadClip(a) {
    const u = a.url;
    if (a.type === "vrma") {
      const { VRMAnimationLoaderPlugin, createVRMAnimationClip } = await import("@pixiv/three-vrm-animation");
      const l = new GLTFLoader(); l.register(parser => new VRMAnimationLoaderPlugin(parser));
      const g = await l.loadAsync(u);
      const va = g.userData.vrmAnimations && g.userData.vrmAnimations[0];
      if (!va) throw new Error("no animation in " + a.name);
      return createVRMAnimationClip(va, vrm);
    }
    const { FBXLoader } = await import("three/addons/loaders/FBXLoader.js");
    const asset = await new FBXLoader().loadAsync(u);
    const src = THREE.AnimationClip.findByName(asset.animations, "mixamo.com") || asset.animations[0];
    if (!src) throw new Error("no animation in " + a.name);
    const tracks = [], restInv = new THREE.Quaternion(), parentRest = new THREE.Quaternion(), q = new THREE.Quaternion(), v = new THREE.Vector3();
    const mHips = asset.getObjectByName("mixamorigHips");
    const hipsH = mHips ? mHips.position.y : 1;
    const vrmHipsH = bones.hips ? Math.abs(bones.hips.getWorldPosition(v).y - vrm.scene.getWorldPosition(new THREE.Vector3()).y) / vrm.scene.scale.y : 1;
    const scaleHips = vrmHipsH / (hipsH || 1);
    const v0 = vrm.meta && vrm.meta.metaVersion === "0";
    for (const track of src.tracks) {
      const [rigName, prop] = track.name.split(".");
      const vrmBone = MIXAMO[rigName.replace(/^mixamorig:?/, "")];
      const node = vrmBone && bone(vrmBone), rigNode = asset.getObjectByName(rigName);
      if (!node || !rigNode) continue;
      rigNode.getWorldQuaternion(restInv).invert();
      rigNode.parent.getWorldQuaternion(parentRest);
      if (track instanceof THREE.QuaternionKeyframeTrack) {
        const vals = track.values.slice();
        for (let i = 0; i < vals.length; i += 4) {
          q.fromArray(vals, i).premultiply(parentRest).multiply(restInv).toArray(vals, i);
        }
        tracks.push(new THREE.QuaternionKeyframeTrack(`${node.name}.${prop}`, track.times, vals.map((x, i) => v0 && i % 2 === 0 ? -x : x)));
      } else if (track instanceof THREE.VectorKeyframeTrack && vrmBone === "hips") {
        // keep the bob, drop walking away: horizontal movement is made relative to the first frame
        const vals = track.values.map((x, i) => (v0 && i % 3 !== 1 ? -x : x) * scaleHips);
        const x0 = vals[0] - hipsRest.x, z0 = vals[2] - hipsRest.z;
        for (let i = 0; i < vals.length; i += 3) { vals[i] -= x0; vals[i + 2] -= z0; }
        tracks.push(new THREE.VectorKeyframeTrack(`${node.name}.${prop}`, track.times, vals));
      }
    }
    return new THREE.AnimationClip(a.name, src.duration, tracks);
  }
  async function setAnimations(list) {
    for (const a of list || []) {
      if (clips.has(a.id)) { clips.get(a.id).when = a.when; continue; }
      try { clips.set(a.id, { name: a.name, when: a.when, clip: await loadClip(a) }); }
      catch (e) { (o.onInfo || console.warn)("Animation " + a.name + ": " + e.message); }
    }
    const keep = new Set((list || []).map(a => a.id));
    for (const id of [...clips.keys()]) if (!keep.has(id)) clips.delete(id);
  }

  // ---- what plays when
  let state = "idle", level = 0, mouth = 0, t = 0, raf = 0, last = performance.now(), fps = 60;
  let visemes = null, visemesAt = 0; const vshape = {};
  let blinkAt = 1.5, blinkT = -1, emotion = null, emotionUntil = 0, vowel = "aa", vowelAt = 0, glance = 0, glanceAt = 2;
  let lastActive = performance.now() / 1000, nextIdleAt = 15 + Math.random() * 20, quietDone = false;
  const sm = { lean: 0, tilt: 0, nod: 0 };
  const emo = { happy: 0, sad: 0, surprised: 0, relaxed: 0 };
  function candidates(tag) {
    const out = [];
    for (const [id, c] of clips) if (c.when === tag) out.push({ kind: "clip", id, name: c.name, clip: c.clip });
    if (o.gestures !== false) for (const [name, g] of Object.entries(GESTURES)) if (g.tags.includes(tag)) out.push({ kind: "gesture", name, g });
    // uploaded animations are preferred when there are any for this moment
    const up = out.filter(x => x.kind === "clip");
    return up.length && Math.random() < 0.8 ? up : out;
  }
  function start(c) {
    if (!c) return false;
    stopAnim(true);
    if (c.kind === "clip") {
      const action = mixer.clipAction(c.clip); action.reset(); action.setLoop(THREE.LoopOnce, 1); action.clampWhenFinished = true; action.play();
      anim = { ...c, action, t0: t, len: c.clip.duration, w: 0, out: false };
    } else anim = { ...c, t0: t, len: c.g.len, w: 1, out: false };
    return true;
  }
  function stopAnim(now) {
    if (!anim) return;
    if (now) { if (anim.action) anim.action.stop(); anim = null; } else anim.out = true;
  }
  let pending = null;   // a gesture to play as soon as she's finished talking
  function trigger(tag) {
    if (state !== "idle" && tag !== "happy") { if (tag === "kiss" || tag === "dance") pending = tag; return; }
    const list = candidates(tag);
    if (list.length) start(list[Math.floor(Math.random() * list.length)]);
  }
  function play(what) {
    for (const [id, c] of clips) if (id === what || c.name === what) return start({ kind: "clip", id, name: c.name, clip: c.clip });
    if (GESTURES[what]) return start({ kind: "gesture", name: what, g: GESTURES[what] });
    return false;
  }
  function react(text) {
    const s = String(text || "").toLowerCase();
    let e = null;
    if (/\b(sorry|unfortunately|sad|can't|cannot|failed|error)\b/.test(s)) e = "sad";
    else if (/(!|\b(great|awesome|glad|love|nice|yay|congrat|happy|perfect|fun)\b)/.test(s)) e = "happy";
    else if (/\b(wow|whoa|really\?|interesting)\b/.test(s)) e = "surprised";
    if (e) { emotion = e; emotionUntil = t + 3.5; }
    if (/\b(love you|good ?night|sweet dreams|kisses?|xoxo|mwah)\b|💋|😘|😚/.test(s)) pending = "kiss";
    else if (/\b(danc(e|es|ing)|let'?s groove|💃)/.test(s) || /💃/.test(s)) pending = "dance";
    else if (e === "happy" && Math.random() < 0.35) setTimeout(() => trigger("happy"), 400);
  }

  const qProc = {}; for (const n in bones) qProc[n] = new THREE.Quaternion();
  const hipsProc = new THREE.Vector3();
  function frame(now) {
    raf = requestAnimationFrame(frame);
    const step = (now - last) / 1000;
    if (light && step < (state === "idle" && !anim ? 1 / 25 : 1 / 31)) return;   // light mode: 30 fps (25 when idle)
    const dt = Math.min(0.05, step); last = now; t += dt;
    fps = lerp(fps, 1 / Math.max(step, 1e-3), 0.05);
    if (state !== "idle") { lastActive = t; quietDone = false; if (anim && anim.kind === "clip" ? true : anim && !(anim.name === "Bounce" || anim.name === "Head tilt")) stopAnim(false); }

    // scheduler: an idle animation every 20–60 s; a stretch after 10 quiet minutes
    if (state === "idle" && !anim && pending) { const tg = pending; pending = null; trigger(tg); lastActive = t; }
    if (state === "idle" && !anim) {
      if (!quietDone && t - lastActive > 600) { quietDone = true; trigger("quiet"); }
      else if (t - lastActive > nextIdleAt) { trigger("idle"); lastActive = t; nextIdleAt = 20 + Math.random() * 40; }
    }

    // 1. the living base pose: relaxed arms, breathing, sway, listening lean, thinking tilt
    resetPose(); relaxed();
    sm.lean = lerp(sm.lean, state === "listening" ? 0.06 : 0, 0.08);
    sm.tilt = lerp(sm.tilt, state === "thinking" ? 0.1 : 0, 0.05);
    sm.nod = lerp(sm.nod, state === "thinking" ? -0.08 : (state === "talking" ? Math.sin(t * 3.1) * 0.03 : 0), 0.1);
    P("spine", sm.lean + Math.sin(t * 1.4) * 0.012, Math.sin(t * 0.37) * 0.025, 0);
    P(bones.chest ? "chest" : "upperChest", Math.sin(t * 1.4 + 0.6) * 0.015, 0, 0);
    P("neck", 0, Math.sin(t * 0.5) * 0.05, sm.tilt + Math.sin(t * 0.31) * 0.02);
    P("head", sm.nod, 0, 0);
    const sway = Math.sin(t * 0.9) * 0.03;
    P("leftUpperArm", 0, 0, sway * A); P("rightUpperArm", 0, 0, -sway * A);

    // 2. a built-in gesture adds on top; an uploaded clip blends in and out
    let extra = {}, look = 0;
    if (anim) {
      const p = clamp((t - anim.t0) / anim.len);
      if (anim.kind === "gesture") {
        let e = smoothstep(Math.min(p / 0.2, (1 - p) / 0.25));
        if (anim.out) { anim.w = Math.max(0, anim.w - dt * 4); e *= anim.w; }
        extra = anim.g.fn(p, e, A) || {};
        for (const [n, r] of Object.entries(extra)) if (Array.isArray(r)) P(n, r[0], r[1], r[2]);
        if (extra.hipsY && bones.hips) bones.hips.position.y += extra.hipsY / vrm.scene.scale.y;
        if (extra.hipsX && bones.hips) bones.hips.position.x += -rightSign * extra.hipsX / vrm.scene.scale.x;   // + = her left
        for (const sd in extra.straighten || {}) {           // open the hand (undo the relaxed finger curl)
          const w = extra.straighten[sd];
          for (const n of FINGERS) if (n.startsWith(sd) && bones[n]) bones[n].quaternion.slerp(new Q(), w);
        }
        for (const ik of extra.ik || []) {
          if (ik.w <= 0) continue;
          const S = bones[ik.side + "UpperArm"]; if (!S) continue;
          const k = (o.height || 172.7) / 170, sx = ik.side === "right" ? rightSign : -rightSign;
          const pole = S.getWorldPosition(new V3()).add(new V3(sx * ik.pole[0] * k, ik.pole[1] * k, ik.pole[2] * k));
          reach(ik.side, ikTargets(ik.target, ik.side), pole, ik.w);
        }
        look = extra.look || 0;
        if (p >= 1 || (anim.out && anim.w <= 0)) anim = null;
      } else {
        for (const n in bones) qProc[n].copy(bones[n].quaternion);
        if (bones.hips) hipsProc.copy(bones.hips.position);
        mixer.update(dt);
        anim.w = anim.out ? Math.max(0, anim.w - dt * 4) : Math.min(1, anim.w + dt * 2.5);
        const fadeEnd = smoothstep((anim.len - (t - anim.t0)) / 0.5);
        const w = anim.w * fadeEnd;
        for (const n in bones) { const b = bones[n]; const aq = b.quaternion.clone(); b.quaternion.copy(qProc[n]).slerp(aq, w); }
        if (bones.hips) bones.hips.position.lerpVectors(hipsProc, bones.hips.position.clone(), w);
        if (p >= 1 || (anim.out && anim.w <= 0)) { anim.action.stop(); anim = null; }
      }
    }

    // eyes: look at the camera, glance away now and then (up when thinking)
    if (t > glanceAt) { glance = Math.random() < 0.5 ? 0 : (Math.random() - 0.5) * 0.5; glanceAt = t + 1.5 + Math.random() * 3; }
    const sc = vrm.scene.scale.y;
    lookTarget.position.lerp(new THREE.Vector3(cam.position.x + (state === "thinking" ? 0.4 : glance) * sc + look,
      cam.position.y + (state === "thinking" ? 0.35 * sc : 0), cam.position.z), 0.08);

    // blinking
    if (t > blinkAt && blinkT < 0) blinkT = 0;
    let blink = 0;
    if (blinkT >= 0) { blinkT += dt; blink = blinkT < 0.07 ? blinkT / 0.07 : blinkT < 0.16 ? 1 - (blinkT - 0.07) / 0.09 : 0;
      if (blinkT >= 0.16) { blinkT = -1; blinkAt = t + 2 + Math.random() * 4; } }
    setEx("blink", Math.max(blink, extra.blink || 0));
    setEx("blinkLeft", extra.blinkLeft || 0); setEx("blinkRight", extra.blinkRight || 0);

    // mouth follows the voice; the vowel shape changes a few times a second
    mouth = lerp(mouth, clamp(level * 1.6), level > mouth ? 0.55 : 0.25);
    const vis = visemes && performance.now() - visemesAt < 250 ? visemes : null;    // mouth shapes from the actual sound
    if (!vis && t > vowelAt) { vowel = ["aa", "aa", "oh", "ih", "ou", "ee"][Math.floor(Math.random() * 6)]; vowelAt = t + 0.09 + Math.random() * 0.12; }
    for (const v of ["aa", "ih", "ou", "ee", "oh"]) {
      const shape = vis ? clamp(mouth * (vis[v] || 0) * 1.4) : (v === vowel ? mouth : (v === "aa" ? mouth * 0.3 : 0));
      vshape[v] = lerp(vshape[v] || 0, shape, 0.45);
      setEx(v, Math.max(vshape[v], extra[v] || 0));
    }

    // feelings fade in and out
    if (t > emotionUntil) emotion = null;
    const goal = { happy: Math.max(emotion === "happy" ? 0.65 : (state === "idle" ? 0.12 : 0), extra.happy || 0),
      sad: emotion === "sad" ? 0.45 : 0, surprised: emotion === "surprised" ? 0.5 : 0,
      relaxed: Math.max(state === "listening" ? 0.25 : 0, extra.relaxed || 0) };
    for (const k in emo) { emo[k] = lerp(emo[k], goal[k], 0.08); setEx(k, emo[k] * (1 - mouth * 0.5)); }

    cam.position.lerp(camGoal, 0.05); aimNow.lerp(camAim, 0.05); cam.lookAt(aimNow);
    vrm.update(dt);
    renderer.render(scene, cam);
  }
  raf = requestAnimationFrame(frame);
  setTimeout(() => trigger("greeting"), 1200);    // a little wave when the screen opens
  setAnimations(o.animations);
  return {
    setState(s) { state = s; },
    setLevel(x) { level = clamp(x); },
    setVisemes(v) { visemes = v; visemesAt = performance.now(); },
    setCamera: m => setCamera(m),
    setHeight,
    setLight(on) { light = on; renderer.setPixelRatio(pixelRatio()); resize(); },
    setAnimations, play, trigger, react,
    fps: () => fps,
    destroy() {
      cancelAnimationFrame(raf); ro.disconnect(); mixer.stopAllAction();
      try { VRMUtils.deepDispose && VRMUtils.deepDispose(vrm.scene); } catch {}
      renderer.dispose(); renderer.domElement.remove();
    },
  };
}

// ------------------------------------------------------------------ wake word (openWakeWord in the browser)
// Same pipeline as the phone app: 80 ms chunks of 16 kHz audio -> mel spectrogram -> embeddings -> wake word score.
let ortLoading = null;
export function loadOrt() {     // ONNX Runtime Web, served by the StellAI server
  if (window.ort) return Promise.resolve(window.ort);
  if (!ortLoading) ortLoading = new Promise((res, rej) => {
    const s = document.createElement("script"); s.src = "/avatar/lib/ort/ort.wasm.min.js";
    s.onload = () => { window.ort.env.wasm.wasmPaths = "/avatar/lib/ort/"; window.ort.env.wasm.numThreads = 1; res(window.ort); };
    s.onerror = () => { ortLoading = null; rej(new Error("the speech engine files aren't downloaded yet")); }; document.head.appendChild(s);
  });
  return ortLoading;
}

// ------------------------------------------------------------------ voice activity detection (Silero VAD)
// prob(frame) -> 0..1 chance that a 512-sample (32 ms, 16 kHz) frame is speech. One model, many streams.
let vadSession = null;
export async function createVad() {
  const ort = await loadOrt();
  if (!vadSession) {
    const r = await fetch("/avatar/lib/vad/silero_vad.onnx");
    if (!r.ok) throw new Error("the speech detector isn't downloaded yet");
    vadSession = await ort.InferenceSession.create(new Uint8Array(await r.arrayBuffer()), { executionProviders: ["wasm"] });
  }
  const sess = vadSession;
  return {
    stream() {
      let state = new Float32Array(2 * 128), ctx = new Float32Array(64), busy = Promise.resolve();
      const sr = new ort.Tensor("int64", BigInt64Array.from([16000n]), []);
      return {
        reset() { state = new Float32Array(2 * 128); ctx = new Float32Array(64); },
        prob(frame) {          // frame: Float32Array(512), -1..1
          const run = async () => {
            const x = new Float32Array(576); x.set(ctx, 0); x.set(frame, 64); ctx = frame.slice(448);
            const out = await sess.run({ input: new ort.Tensor("float32", x, [1, 576]), state: new ort.Tensor("float32", state, [2, 1, 128]), sr });
            state = Float32Array.from(out.stateN.data);
            return out.output.data[0];
          };
          const p = busy.then(run, run); busy = p.catch(() => 0); return p;
        },
      };
    },
  };
}

/** Turns microphone audio at any sample rate into 16 kHz samples. */
export function makeResampler(rate) {
  const ratio = rate / 16000; let pos = 0, carry = new Float32Array(0);
  return input => {
    const all = new Float32Array(carry.length + input.length); all.set(carry, 0); all.set(input, carry.length);
    const out = [];
    while (pos + ratio <= all.length) { let s = 0; const a = Math.floor(pos), b = Math.floor(pos + ratio);
      for (let j = a; j < b; j++) s += all[j]; out.push(s / Math.max(1, b - a)); pos += ratio; }
    const used = Math.floor(pos); carry = all.slice(used); pos -= used;
    return Float32Array.from(out);
  };
}

export async function createWakeWord({ modelUrl, headers = {}, onWake, onError, threshold = 0.5, isBusy = () => false, isSpeaking = () => false,
                                        bargeIn = () => false, onBargeIn = null }) {
  const ort = await loadOrt();
  const fetchModel = async (u, h) => { const r = await fetch(u, { headers: h }); if (!r.ok) throw new Error("couldn't load " + u.split("/").pop()); return new Uint8Array(await r.arrayBuffer()); };
  const opts = { executionProviders: ["wasm"] };
  const mel = await ort.InferenceSession.create(await fetchModel("/avatar/lib/oww/melspectrogram.onnx"), opts);
  const emb = await ort.InferenceSession.create(await fetchModel("/avatar/lib/oww/embedding_model.onnx"), opts);
  const wake = await ort.InferenceSession.create(await fetchModel(modelUrl, headers), opts);
  const FRAMES = 16, CHUNK = 1280, CTX = 480, BINS = 32, WIN = 76;

  let tail = new Float32Array(CTX), melFrames = [], feats = [], preds = 0, cooldownUntil = 0;
  function reset() { melFrames = Array.from({ length: WIN }, () => new Float32Array(BINS).fill(1)); feats = []; preds = 0; }
  reset();
  async function run(sess, data, dims) {
    const out = await sess.run({ [sess.inputNames[0]]: new ort.Tensor("float32", data, dims) });
    return out[sess.outputNames[0]].data;
  }
  async function processChunk(chunk) {     // chunk: 1280 samples as int16-range floats
    const audio = new Float32Array(CTX + CHUNK); audio.set(tail, 0); audio.set(chunk, CTX); tail = audio.slice(audio.length - CTX);
    const m = await run(mel, audio, [1, audio.length]);
    for (let f = 0; f < m.length / BINS; f++) { const fr = new Float32Array(BINS); for (let b = 0; b < BINS; b++) fr[b] = m[f * BINS + b] / 10 + 2; melFrames.push(fr); }
    if (melFrames.length > 200) melFrames.splice(0, melFrames.length - 200);
    const win = new Float32Array(WIN * BINS), st = melFrames.length - WIN;
    for (let i = 0; i < WIN; i++) win.set(melFrames[st + i], i * BINS);
    feats.push(Float32Array.from(await run(emb, win, [1, WIN, BINS, 1])));
    if (feats.length > FRAMES * 2) feats.splice(0, feats.length - FRAMES * 2);
    if (feats.length < FRAMES) return 0;
    const dim = feats[0].length, inp = new Float32Array(FRAMES * dim), s0 = feats.length - FRAMES;
    for (let i = 0; i < FRAMES; i++) inp.set(feats[s0 + i], i * dim);
    const score = (await run(wake, inp, [1, FRAMES, dim]))[0];
    return ++preds <= 5 ? 0 : score;
  }

  const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const src = ctx.createMediaStreamSource(stream), proc = ctx.createScriptProcessor(4096, 1, 1);
  const resample = makeResampler(ctx.sampleRate); let pending = [], working = false, stopped = false;
  // Interrupting by talking: while Stella speaks, a speech detector listens for you (needs echo cancellation)
  let vad = null, vbuf = [], speechRun = 0, vadBusy = false;
  if (onBargeIn) createVad().then(v => { vad = v.stream(); }).catch(() => {});
  async function bargeCheck(samples) {
    if (!vad || !isSpeaking() || !bargeIn() || isBusy()) { vbuf = []; speechRun = 0; return; }
    for (const x of samples) vbuf.push(x);
    if (vadBusy) return; vadBusy = true;
    try {
      while (vbuf.length >= 512) {
        const p = await vad.prob(Float32Array.from(vbuf.splice(0, 512)));
        speechRun = p > 0.8 ? speechRun + 1 : Math.max(0, speechRun - 2);
        if (speechRun >= 12) { speechRun = 0; vbuf = []; vad.reset(); onBargeIn(); break; }   // ~0.4 s of clear speech
      }
    } catch {} finally { vadBusy = false; }
  }
  proc.onaudioprocess = ev => {
    if (stopped) return;
    const out = resample(ev.inputBuffer.getChannelData(0));
    bargeCheck(out);
    for (const x of out) pending.push(x * 32767);
    if (pending.length > CHUNK * 6) pending.splice(0, pending.length - CHUNK * 6);   // falling behind: drop old audio
    pump();
  };
  async function pump() {
    if (working) return; working = true;
    try {
      while (pending.length >= CHUNK && !stopped) {
        const chunk = Float32Array.from(pending.splice(0, CHUNK));
        if (isBusy()) { reset(); continue; }                 // already recording a question
        const score = await processChunk(chunk);
        const need = isSpeaking() ? Math.min(0.92, threshold + 0.3) : threshold;  // harder while Stella talks
        if (score >= need && performance.now() > cooldownUntil) { cooldownUntil = performance.now() + 2000; reset(); onWake && onWake(score); }
      }
    } catch (e) { onError && onError(e); }
    working = false;
  }
  src.connect(proc); proc.connect(ctx.destination);
  return {
    setThreshold(x) { threshold = x; },
    stop() { stopped = true; try { proc.disconnect(); src.disconnect(); } catch {} stream.getTracks().forEach(tr => tr.stop()); ctx.close(); },
  };
}
