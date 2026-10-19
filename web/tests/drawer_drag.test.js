import test from 'node:test';
import assert from 'node:assert/strict';
import { BLOCKING_SELECTOR, DRAG, DRAWER_DRAG, INTERACTIVE_SELECTOR, SIDEWAYS_SELECTOR, bindDrawerDrag } from '../modules/drawer_drag.js';

// The drawer's finger (owner Q1=A): eligibility, the axis lock, tracking by one number, the commit rules,
// the restores that leave no trace, the swallowed ending click, and the one-line switch.

function fakeEl(name) {
    const classes = new Set();
    const props = new Map();
    return {
        name, hidden: true, parentElement: null, scrollWidth: 0, clientWidth: 0, style: null, captured: null,
        classList: { toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)), contains: (c) => classes.has(c), add: (c) => classes.add(c) },
        styleProps: props,
        contains(other) { for (let x = other; x; x = x.parentElement) if (x === this) return true; return false; },
        closest() { return null; },
        getBoundingClientRect: () => ({ width: 320 }),
        setPointerCapture(id) { this.captured = id; }, releasePointerCapture() { this.captured = null; },
    };
}
function withStyle(el) { el.style = { setProperty: (k, v) => el.styleProps.set(k, v), removeProperty: (k) => el.styleProps.delete(k) }; return el; }

function world({ open = false, stack = true, coarse = true, enabled = DRAWER_DRAG, blockers = null } = {}) {
    const listeners = new Map();
    const add = (map) => (type, fn) => { map.set(type, [...(map.get(type) || []), fn]); };
    const remove = (map) => (type, fn) => { map.set(type, (map.get(type) || []).filter((x) => x !== fn)); };
    const docListeners = new Map();
    const styles = new Map();
    const doc = { body: fakeEl('body'), addEventListener: add(docListeners), removeEventListener: remove(docListeners), listeners: docListeners,
        querySelectorAll: () => blockers || [] };
    const win = { addEventListener: add(listeners), removeEventListener: remove(listeners), listeners,
        getComputedStyle: (el) => styles.get(el) || { touchAction: 'auto', overflowX: 'visible' }, performance: { now: () => clock.t } };
    const clock = { t: 1000 };
    const sidebar = withStyle(fakeEl('sidebar'));
    const scrim = withStyle(fakeEl('scrim'));
    const room = fakeEl('room'); room.parentElement = doc.body;
    const state = { open, set: [] };
    const flags = { interactive: new Set(), sideways: new Set(), blocked: false, selection: false };
    const dispose = bindDrawerDrag({
        sidebar, scrim, isOpen: () => state.open,
        setOpen: (v) => { state.set.push(v); state.open = v; scrim.hidden = !v; },
        isStack: () => stack, isCoarse: () => coarse, win, doc, enabled,
        isInteractive: (el) => flags.interactive.has(el), isSideways: (el) => flags.sideways.has(el),
        ...(blockers ? {} : { isBlocked: () => flags.blocked }), hasSelection: () => flags.selection,
    });
    const fire = (type, event) => (docListeners.get(type) || []).forEach((fn) => fn(event));
    const pointer = (type, { x, y, id = 1, pointerType = 'touch', target = room, isPrimary = true, dt = 16 }) => {
        clock.t += dt;
        const event = { type, pointerId: id, pointerType, isPrimary, clientX: x, clientY: y, target, timeStamp: clock.t };
        fire(type, event);
        return event;
    };
    const click = () => { const e = { stopped: false, prevented: false, stopPropagation() { this.stopped = true; }, preventDefault() { this.prevented = true; } }; fire('click', e); return e; };
    const progress = () => Number(sidebar.styleProps.get('--drawer-p'));
    const dragging = () => sidebar.classList.contains('dragging') && scrim.classList.contains('dragging');
    return { doc, win, sidebar, scrim, room, state, flags, styles, dispose, pointer, click, fire, progress, dragging, clock };
}

// A drag from `from`: the first move locks the axis one slop in (12px), then `steps` moves reach `to`.
// Progress is measured from the lock point, so p = (to - from - 12) / 320.
const open = (w, { from = 100, to = 300, steps = 4, dt = 16, up = true } = {}) => {
    w.pointer('pointerdown', { x: from, y: 300 });
    const lock = from + 12;
    w.pointer('pointermove', { x: lock, y: 300, dt });
    for (let i = 1; i <= steps; i += 1) w.pointer('pointermove', { x: lock + ((to - lock) * i) / steps, y: 300, dt });
    if (up) w.pointer('pointerup', { x: to, y: 300, dt });
};

test('the switch: DRAWER_DRAG off binds nothing', () => {
    assert.equal(typeof DRAWER_DRAG, 'boolean');
    const w = world({ enabled: false });
    assert.equal(w.doc.listeners.size, 0);
    w.dispose();
});

test('a start is eligible only on a quiet surface, past the edge, with nothing over the room, from a touch', () => {
    const w = world();
    const tryOpen = (opts = {}) => { open(w, opts); const set = w.state.set.slice(); w.state.set.length = 0; return set; };
    assert.deepEqual(tryOpen(), [true], 'the plain case opens');
    w.state.open = false; w.scrim.hidden = true;
    assert.deepEqual(tryOpen({ from: 20, to: 300 }), [], 'under 24px from the edge belongs to the OS');
    w.flags.interactive.add(w.room);
    assert.deepEqual(tryOpen(), [], 'a control keeps its touch');
    w.flags.interactive.clear(); w.flags.sideways.add(w.room);
    assert.deepEqual(tryOpen(), [], 'a sideways scroller keeps its touch');
    w.flags.sideways.clear(); w.flags.selection = true;
    assert.deepEqual(tryOpen(), [], 'selected text keeps its touch');
    w.flags.selection = false; w.flags.blocked = true;
    assert.deepEqual(tryOpen(), [], 'a dialog, menu, reader or Shade owns the finger');
    w.flags.blocked = false;
    w.styles.set(w.room, { touchAction: 'none', overflowX: 'visible' });
    assert.deepEqual(tryOpen(), [], 'touch-action: none (the effort strip) keeps its touch');
    w.styles.set(w.room, { touchAction: 'auto', overflowX: 'auto' }); w.room.scrollWidth = 900; w.room.clientWidth = 300;
    assert.deepEqual(tryOpen(), [], 'a horizontal scroller keeps its touch');
    w.styles.delete(w.room); w.room.scrollWidth = 0;
    w.pointer('pointerdown', { x: 100, y: 300, pointerType: 'mouse' });
    w.pointer('pointermove', { x: 300, y: 300, pointerType: 'mouse' }); w.pointer('pointerup', { x: 300, y: 300, pointerType: 'mouse' });
    assert.deepEqual(w.state.set, [], 'a mouse never drags the drawer');
    w.dispose();
    const docked = world({ stack: false });
    open(docked); assert.deepEqual(docked.state.set, [], 'docked: no drawer to drag'); docked.dispose();
    const fine = world({ coarse: false });
    open(fine); assert.deepEqual(fine.state.set, [], 'a fine pointer: no drag'); fine.dispose();
    // The real blocker test reads what is shown: a closed menu that stays mounted (Files' context menu, a model
    // list) does not block; an open one does.
    const mounted = world({ blockers: [{ getClientRects: () => [] }] });
    open(mounted); assert.deepEqual(mounted.state.set, [true], 'a mounted, hidden menu does not block'); mounted.dispose();
    const shown = world({ blockers: [{ getClientRects: () => [] }, { getClientRects: () => [{}] }] });
    open(shown); assert.deepEqual(shown.state.set, [], 'an open menu owns the finger'); shown.dispose();
    assert.match(INTERACTIVE_SELECTOR, /input, textarea/); assert.match(SIDEWAYS_SELECTOR, /\[data-scroll-x\]/); assert.match(BLOCKING_SELECTOR, /dialog\[open\]/);
});

test('the axis locks after the slop: vertical movement is the page, sideways is the drawer, tracked by one number', () => {
    const w = world();
    w.pointer('pointerdown', { x: 100, y: 300 });
    w.pointer('pointermove', { x: 104, y: 306 });
    assert.equal(w.dragging(), false, 'inside the slop nothing is decided');
    w.pointer('pointermove', { x: 110, y: 330 });
    w.pointer('pointermove', { x: 300, y: 330 });
    assert.equal(w.dragging(), false, 'a vertical start is a scroll, never a drag');
    w.pointer('pointerup', { x: 300, y: 330 });
    assert.deepEqual(w.state.set, []);
    // Sideways past the slop, with |dx| > 1.3·|dy|: the drawer is revealed at 0 and follows.
    w.pointer('pointerdown', { x: 100, y: 300 });
    w.pointer('pointermove', { x: 112, y: 304 });
    assert.equal(w.dragging(), true);
    assert.equal(w.scrim.hidden, false, 'the scrim is painted during the drag');
    assert.equal(w.progress(), 0);
    assert.equal(w.room.captured, 1, 'the pointer is captured on its target');
    w.pointer('pointermove', { x: 112 + 160, y: 304 });
    assert.equal(w.progress(), 0.5, 'progress is travel over the drawer width from the lock point');
    assert.equal(w.scrim.styleProps.get('--drawer-p'), '0.5');
    w.pointer('pointermove', { x: 112 + 900, y: 304 });
    assert.equal(w.progress(), 1, 'clamped');
    w.pointer('pointerup', { x: 112 + 900, y: 304 });
    assert.deepEqual(w.state.set, [true]);
    assert.equal(w.dragging(), false);
    assert.equal(w.sidebar.styleProps.has('--drawer-p'), false, 'the number is the finger\'s; the state owns the rest');
    w.dispose();
});

test('a slow release commits past half (open) or 30% (close); a fast one commits after 24px; else it springs back', () => {
    let w = world();
    open(w, { from: 100, to: 100 + 12 + 150, steps: 5, dt: 200 });  // slow: 30px per 200ms, p = 0.47
    assert.deepEqual(w.state.set, [], 'under half, slow: springs back');
    assert.equal(w.scrim.hidden, true, 'and the scrim is put away');
    open(w, { from: 100, to: 100 + 12 + 170, steps: 5, dt: 200 });  // slow, p = 0.53
    assert.deepEqual(w.state.set, [true]);
    w.dispose();
    w = world();
    open(w, { from: 100, to: 100 + 12 + 40, steps: 2, dt: 16 });  // fast: 20px per 16ms, 52px of travel
    assert.deepEqual(w.state.set, [true], 'a fling after 24px opens');
    w.dispose();
    w = world();
    w.pointer('pointerdown', { x: 100, y: 300 });
    w.pointer('pointermove', { x: 115, y: 300, dt: 1 });
    w.pointer('pointermove', { x: 122, y: 300, dt: 1 });
    w.pointer('pointerup', { x: 122, y: 300, dt: 1 });
    assert.deepEqual(w.state.set, [], 'a fast flick under 24px of travel is not a commit');
    w.dispose();
    // Closing: a drag on the open drawer, leftwards.
    w = world({ open: true });
    w.scrim.hidden = false;
    const close = (to, dt) => {
        w.pointer('pointerdown', { x: 300, y: 300, target: w.sidebar });
        w.pointer('pointermove', { x: 288, y: 300, dt });
        w.pointer('pointermove', { x: to, y: 300, dt });
        w.pointer('pointerup', { x: to, y: 300, dt });
    };
    close(288 - 60, 200);
    assert.deepEqual(w.state.set, [], 'less than 30% gone, slow: stays open');
    close(288 - 110, 200);
    assert.deepEqual(w.state.set, [false], 'more than 30% gone: closes');
    w.state.open = true; w.state.set.length = 0;
    w.pointer('pointerdown', { x: 300, y: 300, target: w.scrim });
    w.pointer('pointermove', { x: 320, y: 300 });
    assert.equal(w.dragging(), false, 'the open drawer dragged rightwards has nowhere to go');
    w.pointer('pointerup', { x: 320, y: 300 });
    w.pointer('pointerdown', { x: 300, y: 300, target: w.room });
    w.pointer('pointermove', { x: 200, y: 300 });
    assert.equal(w.dragging(), false, 'a start outside the open drawer and its scrim is not a close');
    w.pointer('pointerup', { x: 200, y: 300 });
    assert.deepEqual(w.state.set, []);
    w.dispose();
});

test('a second finger, pointercancel, lost capture or a resize restore the start with no commit', () => {
    const w = world();
    const start = () => { w.pointer('pointerdown', { x: 100, y: 300 }); w.pointer('pointermove', { x: 112, y: 300 }); w.pointer('pointermove', { x: 260, y: 300 }); assert.equal(w.dragging(), true); };
    start(); w.pointer('pointerdown', { x: 200, y: 400, id: 2, isPrimary: false });  // a real second touch is not primary
    assert.equal(w.dragging(), false); assert.equal(w.scrim.hidden, true);
    start(); w.fire('pointercancel', { pointerId: 1 });
    assert.equal(w.dragging(), false); assert.equal(w.scrim.hidden, true);
    start(); w.fire('lostpointercapture', { pointerId: 1 });
    assert.equal(w.dragging(), false);
    start(); (w.win.listeners.get('resize') || []).forEach((fn) => fn());
    assert.equal(w.dragging(), false);
    assert.deepEqual(w.state.set, [], 'no state change, so no history entry');
    w.pointer('pointerup', { x: 260, y: 300 });
    assert.deepEqual(w.state.set, []);
    w.dispose();
});

test('the click that ends a drag is swallowed once; a tap is not', () => {
    const w = world();
    w.pointer('pointerdown', { x: 100, y: 300 });
    w.pointer('pointerup', { x: 100, y: 300 });
    assert.equal(w.click().stopped, false, 'a tap keeps its click');
    open(w, { from: 100, to: 400 });
    const ending = w.click();
    assert.equal(ending.stopped && ending.prevented, true);
    assert.equal(w.click().stopped, false, 'only the one ending click');
    w.clock.t += DRAG.clickGuard + 1;
    w.dispose();
    assert.equal([...w.doc.listeners.values()].every((fns) => fns.length === 0), true, 'dispose releases every listener');
    assert.equal([...w.win.listeners.values()].every((fns) => fns.length === 0), true);
});
