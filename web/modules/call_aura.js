/** The wave in the glass: the call's light is the owner's wave, drawn inside the composer capsule's
 * frosted glass. Three strands of a sine whose envelope is zero at both ends (`STRANDS`, `waveShape`)
 * run as tapered ribbons across the capsule's width, behind the words (stepping back under them, so
 * they stay readable). One voice's colour at a time: the strands and a faint tint of the glass take
 * the speaker's hue (owner blue, Ouroboros crimson) and change it by fading through the neutral,
 * never two hues lit together. The glass's outline breathes with the heard level, a spring of a few
 * pixels that never moves the layout. Thinking is a soft travelling pulse; working draws the strands
 * in to the room's three dots and back; muted is one dashed neutral line; an error holds still in
 * amber. In the big view (`data-call-view="big"`) the same glass and wave are larger.
 *
 * A visual is one contract, so another replaces this module and its call.css block and nothing
 * else: `mount` fills the capsule's slot (here the wave's canvas and the work dots), may add layers
 * to the row (here the glass behind it) and draws one frame when the light asks (`draw`); `mark` is
 * the small static sign of Talk and of the shell's mark. The light writes `--call-mic`/`--call-out`
 * on the composer and the capsule writes `data-call-light|voice|tone|view|work`.
 */
// Three strands of the wave: [opacity, cycles across the wave, phase].
export const STRANDS = [[1, 1.9, 0], [0.6, 2.7, 1.3], [0.35, 3.6, 2.4]];
// The level's last half second, travelling outward from the wave's middle.
const HISTORY = 26;
const HISTORY_STEP = 0.024;
// After a change of size or a still state the visual paints its own frames this long, so springs settle.
const SETTLE_SECONDS = 0.9;
const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
const ease = (current, target, dt, up, down) => current + (target - current) * (1 - Math.exp(-Math.max(0, dt) / (target > current ? up : down)));
const mix = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
const rgba = (c, alpha) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${clamp(alpha, 0, 1).toFixed(3)})`;
const triplet = (text, fallback) => {
    const numbers = String(text || '').match(/[\d.]+/g);
    return numbers && numbers.length >= 3 ? numbers.slice(0, 3).map(Number) : fallback;
};

/** What the wave shows for the light's state: whose colour, how high, a pulse or a voice. */
export function waveShape({ mic = 0, out = 0, time = 0, mode = 'listen', voice = 'owner', owner = false, reduced = false }) {
    const heardOuroboros = voice === 'ouroboros' && out > 0.02;
    const colour = mode === 'error' ? 'error'
        : heardOuroboros || mode === 'think' ? 'ouroboros'
            : mode !== 'muted' && (owner || (mic > 0.08 && mic >= out)) ? 'owner' : 'idle';
    if (reduced || mode === 'error' || mode === 'still') return { colour, amp: 0, pulse: false };
    if (mode === 'muted') return { colour, amp: heardOuroboros ? out : 0, pulse: false };
    if (mode === 'think' || mode === 'trace') return { colour, amp: 0, pulse: true };
    const idle = mode === 'listen' ? 0.07 + 0.03 * Math.sin(time * 2.2) : 0;
    return { colour, amp: Math.max(voice === 'ouroboros' ? out : Math.max(mic, out), idle), pulse: false };
}

/** Talk's sign: the three strands in miniature, still. */
function waveMark(doc) {
    const mark = doc.createElement('span');
    mark.className = 'call-entry-mark';
    mark.setAttribute('aria-hidden', 'true');
    const path = ([alpha, cycles, phase]) => {
        let d = '';
        for (let k = 0; k <= 36; k += 1) {
            const u = k / 36;
            const y = 7 + 4.8 * alpha * Math.sin(Math.PI * u) ** 2 * Math.sin(2 * Math.PI * cycles * u + phase);
            d += `${k ? 'L' : 'M'}${(1.2 + u * 17.6).toFixed(2)} ${y.toFixed(2)}`;
        }
        return d;
    };
    mark.innerHTML = '<svg width="20" height="14" viewBox="0 0 20 14" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round">'
        + `<path d="${path(STRANDS[2])}" stroke-width="1" opacity="0.4"/><path d="${path(STRANDS[1])}" stroke-width="1.3" opacity="0.65"/>`
        + `<path d="${path(STRANDS[0])}" stroke-width="1.8"/></svg>`;
    return mark;
}

function mount({ slot, row, wrap, doc, env = globalThis }) {
    // The glass: the row's own surface while a call is live, tinted by one hue, breathing outward.
    const glass = doc.createElement('span');
    glass.className = 'call-glass';
    glass.setAttribute('aria-hidden', 'true');
    glass.innerHTML = '<span class="call-glass-tint call-glass-owner"></span><span class="call-glass-tint call-glass-ouroboros"></span>';
    row.append(glass);
    const canvas = doc.createElement('canvas');
    canvas.className = 'call-wave';
    canvas.setAttribute('aria-hidden', 'true');
    const work = doc.createElement('span');
    work.className = 'call-work chat-live-typing';
    work.setAttribute('aria-hidden', 'true');
    work.innerHTML = '<span></span><span></span><span></span>';
    slot.replaceChildren(canvas, work);
    const context = canvas.getContext?.('2d') || null;
    const ratio = Math.min(2, Math.max(1, Number(env.devicePixelRatio) || 1));
    const big = () => wrap.dataset.callView === 'big';

    // ---- where the wave lives: measured on a change of size, never per frame ----
    let width = 300;
    let height = 52;
    let x0 = 20;
    let x1 = 240;
    let centre = 0.5;
    let mounted = false;
    function fit() {
        width = Math.max(24, slot.clientWidth || 0);
        height = Math.max(16, slot.clientHeight || 0);
        if (canvas.width !== Math.round(width * ratio)) canvas.width = Math.round(width * ratio);
        if (canvas.height !== Math.round(height * ratio)) canvas.height = Math.round(height * ratio);
        const edge = slot.getBoundingClientRect?.() || { left: 0 };
        const buttons = row.querySelector('.call-buttons')?.getBoundingClientRect?.();
        const words = row.querySelector('.call-words');
        let wordsEnd = 0;
        if (wrap.dataset.callView === 'sheet') { x0 = 3; x1 = width - 3; } else if (big()) { x0 = 28; x1 = width - 28; } else {
            x0 = 20;
            x1 = buttons?.width ? Math.max(x0 + 40, buttons.left - edge.left - 10) : width - 20;
            const box = words?.getBoundingClientRect?.();
            if (box?.width) {
                const text = Math.min(box.width, Math.max(...[...words.children].filter(child => !child.hidden)
                    .map(child => child.scrollWidth || 0), 0) + 8);
                wordsEnd = Math.max(0, box.left - edge.left + text);
                // Room enough: the wave starts where the words end; else it runs under them, stepping back.
                if (x1 - (wordsEnd + 20) >= 170) { x0 = wordsEnd + 20; wordsEnd = 0; } else x0 = 14;
            }
        }
        canvas.style.setProperty('--call-words', `${Math.round(wordsEnd)}px`);
        centre = big() && (env.innerWidth || 0) >= 980 ? 0.54 : 0.5;
        work.style.setProperty('--call-work-x', `${Math.round((x0 + x1) / 2)}px`);
        work.style.setProperty('--call-work-y', `${(centre * 100).toFixed(1)}%`);
        settle();
        if (mounted) frame();  // a resized canvas is blank: paint it before the browser does
    }
    const sizing = typeof env.ResizeObserver === 'function' ? new env.ResizeObserver(fit) : null;
    for (const element of [slot, row, row.querySelector('.call-buttons'), row.querySelector('.call-words')])
        if (element) sizing?.observe(element);

    let colours = null;  // read from the stylesheet once per theme, never per frame
    const recolour = () => { colours = null; frame(); };
    env.addEventListener?.('ouro:theme-changed', recolour);
    function palette() {
        if (!colours) {
            const style = env.getComputedStyle?.(wrap);
            const read = (name, fallback) => triplet(style?.getPropertyValue(name), fallback);
            colours = { owner: read('--call-owner-rgb', [47, 107, 214]), ouroboros: read('--call-ouro-rgb', [201, 53, 69]),
                error: read('--call-amber-rgb', [180, 83, 9]), neutral: read('--neutral-rgb', [28, 33, 51]),
                rest: Number(style?.getPropertyValue('--call-wave-rest')) || 0.5,
                band: Number(style?.getPropertyValue('--call-wave-band')) || 0.12 };
        }
        return colours;
    }

    // ---- the voice, as the wave carries it ----
    const history = new Float32Array(HISTORY);
    let historyClock = 0;
    const heardAt = (distance) => {
        const x = clamp(distance, 0, 1) * (HISTORY - 1);
        const i = Math.floor(x);
        return history[i] + (history[Math.min(HISTORY - 1, i + 1)] - history[i]) * (x - i);
    };
    let amp = 0;          // the level's spring
    let velocity = 0;
    let phase = 0;        // the travelling phase
    let busy = 0;         // working: the strands draw in to the three dots
    let hue = 'idle';     // the colour in use; it fades through the neutral to change
    let hueLevel = 1;
    let breath = 0;       // the outline's spring
    let breathVelocity = 0;
    const tint = { owner: 0, ouroboros: 0 };
    const written = new Map();
    const put = (target, name, value) => {
        if (written.get(name) === value) return;
        written.set(name, value);
        target.style.setProperty(name, value);
    };
    const now = () => (env.performance?.now?.() ?? Date.now()) / 1000;
    let input = null;
    let clock = 0;
    let settling = 0;
    let settleUntil = 0;

    const points = [];
    function line([alpha, cycles, offset], shape, cy, reach, spanScale, time) {
        // One strand's centre line across the span, sampled every ~3 px.
        const span = x1 - x0;
        const n = Math.max(24, Math.min(260, Math.round(span / 3)));
        const k = clamp(span / 300, 1, big() ? 2.4 : 2);
        points.length = 0;
        for (let i = 0; i <= n; i += 1) {
            const u = i / n;
            const uu = (u - 0.5) / spanScale + 0.5;  // working draws the span in to its middle
            const envelope = uu <= 0 || uu >= 1 ? 0 : Math.sin(Math.PI * uu) ** 2;
            let y;
            if (shape.pulse) {
                y = Math.sin(uu * Math.PI * 2 * cycles * k - time * 7 + offset) * reach * envelope * alpha
                    * (0.14 + 0.2 * Math.max(0, Math.sin(uu * 6 - time * 5)));
            } else {
                const local = amp * 0.42 + 0.58 * heardAt(Math.abs(uu - 0.5) * 2) * (amp > 0.005 ? 1 : 0);
                y = Math.sin(uu * Math.PI * 2 * cycles * k + phase + offset) * reach * envelope * alpha * local;
            }
            points.push(x0 + u * span, cy - y);
        }
        return points;
    }

    function ribbon(p, colour, alpha, body, tip) {
        // A tapered ribbon along the line: widest in the middle, round caps at the ends.
        const n = p.length / 2;
        context.beginPath();
        for (let pass = 0; pass < 2; pass += 1) {
            for (let k = 0; k < n; k += 1) {
                const i = pass ? n - 1 - k : k;
                const a = Math.max(0, i - 1);
                const b = Math.min(n - 1, i + 1);
                const tx = p[b * 2] - p[a * 2];
                const ty = p[b * 2 + 1] - p[a * 2 + 1];
                const length = Math.hypot(tx, ty) || 1;
                const half = (tip + (body - tip) * Math.sin(Math.PI * (i / (n - 1))) ** 0.6) / 2;
                const side = pass ? -half : half;
                const px = p[i * 2] - (ty / length) * side;
                const py = p[i * 2 + 1] + (tx / length) * side;
                if (!pass && !k) context.moveTo(px, py); else context.lineTo(px, py);
            }
        }
        context.closePath();
        context.fillStyle = rgba(colour, alpha);
        context.fill();
        for (const i of [0, n - 1]) {
            context.beginPath();
            context.arc(p[i * 2], p[i * 2 + 1], tip / 2, 0, Math.PI * 2);
            context.fill();
        }
    }

    function frame() {
        if (!input) return;
        const t = now();
        const dt = clock ? Math.min(0.1, Math.max(0, t - clock)) : 1 / 60;
        clock = t;
        const shape = waveShape(input);
        const reduced = Boolean(input.reduced);
        const large = big();
        const level = shape.amp;
        const working = input.mode === 'think' && wrap.dataset.callWork === '1';

        historyClock += dt;
        while (historyClock >= HISTORY_STEP) { historyClock -= HISTORY_STEP; history.copyWithin(1, 0, HISTORY - 1); }
        history[0] = reduced ? 0 : level;
        // A spring on the level: quick to rise, a little overshoot, lets go gently.
        velocity += (220 * (level - amp) - 24 * velocity) * dt;
        amp = reduced ? level : Math.max(0, amp + velocity * dt);
        phase += reduced ? 0 : dt * (4 + 6 * amp);
        busy = reduced ? (working ? 1 : 0) : ease(busy, working ? 1 : 0, dt, 0.28, 0.22);

        // One hue at a time: a new speaker's colour arrives after the old one faded to the neutral.
        if (shape.colour !== hue) {
            hueLevel = reduced ? 0 : ease(hueLevel, 0, dt, 0.1, 0.11);
            if (hueLevel < 0.07) hue = shape.colour;
        } else hueLevel = reduced ? 1 : ease(hueLevel, 1, dt, 0.2, 0.2);
        const colours = palette();
        const ink = hue === 'idle' ? colours.neutral
            : mix(colours.neutral, hue === 'owner' ? colours.owner : hue === 'ouroboros' ? colours.ouroboros : colours.error, hueLevel);
        const quiet = hue === 'idle' || (hue !== shape.colour && hueLevel < 0.5);

        // The glass: tinted by the hue in use, its outline breathing with the level.
        const lit = clamp(amp * 1.4, 0, 1) * (working ? 0.5 : 1) * (hue === 'idle' ? 0 : hueLevel);
        const tintTarget = name => (hue === name && !working ? 0.18 + 0.82 * lit : hue === name ? 0.2 * hueLevel : 0);
        for (const [name, other] of [['owner', 'ouroboros'], ['ouroboros', 'owner']]) {
            // The incoming voice tints the glass only once the outgoing one has cleared it.
            const target = tint[other] > 0.03 ? 0 : tintTarget(name);
            tint[name] = reduced ? (hue === name ? 0.5 : 0) : ease(tint[name], target, dt, 0.25, 0.18);
        }
        const reach = reduced ? 0 : (large ? 8 : 4.5) * clamp(amp * 1.25, 0, 1);
        breathVelocity += (260 * (reach - breath) - 20 * breathVelocity) * dt;
        breath = reduced ? 0 : Math.max(0, breath + breathVelocity * dt);
        put(row, '--call-breath', breath.toFixed(2));
        put(row, '--call-tint-owner', tint.owner.toFixed(3));
        put(row, '--call-tint-ouroboros', tint.ouroboros.toFixed(3));
        const dots = busy > 0.55 ? '1' : '';
        if ((slot.dataset.work || '') !== dots) slot.dataset.work = dots;
        const sheet = wrap.dataset.callView === 'sheet';
        if (!context || !(wrap.dataset.call === 'live' || (sheet && input.mode === 'error'))) return;

        // ---- the three strands ----
        context.setTransform(ratio, 0, 0, ratio, 0, 0);
        context.clearRect(0, 0, width, height);
        const cy = height * centre;
        const span = x1 - x0;
        const reachPx = large ? Math.min(height * 0.3, 190) : height * (span < 200 ? 0.46 : 0.4);
        const tip = large ? 0.9 : 0.7;
        if (input.mode === 'error') {
            ribbon(line(STRANDS[0], { pulse: false }, cy, 0, 1, t), colours.error, 0.95, large ? 3.2 : 2.2, tip);
            return;
        }
        if (input.mode === 'muted' && shape.amp === 0) {
            // Muted: one quiet dashed line in the room's ink.
            context.beginPath();
            context.moveTo(x0, cy);
            context.lineTo(x1, cy);
            context.strokeStyle = rgba(colours.neutral, colours.rest * 0.9);
            context.lineWidth = large ? 2.4 : 1.8;
            context.lineCap = 'round';
            context.setLineDash?.([0.1, large ? 8 : 6]);
            context.stroke();
            context.setLineDash?.([]);
            return;
        }
        if (busy > 0.98) return;  // the three dots stand where the strands drew in
        const gone = 1 - busy * 0.97;
        const spanScale = 1 - 0.92 * busy;
        const restBoost = reduced && hue !== 'idle' ? 1.5 : 1;
        for (let s = STRANDS.length - 1; s >= 0; s -= 1) {
            const strand = STRANDS[s];
            const p = line(strand, shape, cy, reachPx, spanScale, t);
            const alpha = strand[0] * (quiet ? colours.rest : 0.96) * gone;
            const body = ((large ? 2.1 : 1.35) + (large ? 3.6 : 2) * strand[0]) * restBoost * (quiet ? 0.9 : 1);
            if (!quiet && s === 0 && !shape.pulse) {
                // The glass's own light: a soft body between the main strand and its mirror.
                context.beginPath();
                for (let i = 0; i < p.length; i += 2) { if (i) context.lineTo(p[i], p[i + 1]); else context.moveTo(p[i], p[i + 1]); }
                for (let i = p.length - 2; i >= 0; i -= 2) context.lineTo(p[i], cy + (cy - p[i + 1]));
                context.closePath();
                context.fillStyle = rgba(ink, 0.07 * lit * gone);
                context.fill();
            }
            if (!quiet) ribbon(p, ink, alpha * colours.band * 1.6, body * 3.6, tip * 2);
            ribbon(p, ink, alpha, body, tip);
        }
    }

    function settleFrame() {
        settling = 0;
        if (now() > settleUntil) return;
        frame();
        settling = env.requestAnimationFrame?.(settleFrame) || 0;
    }
    function settle() {
        settleUntil = Math.max(settleUntil, now() + SETTLE_SECONDS);
        if (!settling && input) settling = env.requestAnimationFrame?.(settleFrame) || 0;
    }

    fit();
    mounted = true;
    return {
        draw(state) {
            input = state;
            frame();
            // Still states stop the light's own frames: the springs finish easing on a few of ours.
            if (['muted', 'error', 'still'].includes(state.mode) || wrap.dataset.callMove === '1') settle();
        },
        destroy() {
            if (settling) env.cancelAnimationFrame?.(settling);
            sizing?.disconnect();
            env.removeEventListener?.('ouro:theme-changed', recolour);
            slot.replaceChildren();
            delete slot.dataset.work;
            glass.remove();
            for (const name of ['--call-breath', '--call-tint-owner', '--call-tint-ouroboros']) row.style.removeProperty?.(name);
        },
    };
}

export const CALL_VISUAL = Object.freeze({ name: 'glasswave', mount, mark: waveMark });
