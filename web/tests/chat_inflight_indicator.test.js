import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
    computeDerivedChatStatus,
    computeHydratedDirectActivities,
} from '../modules/chat.js';
import {
    createStateSnapshotSequencer,
    isForegroundLiveCard,
    routingAnnotationText,
} from '../modules/chat_activity.js';

const chatSource = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');

test('review-only owner anchors do not advertise foreground task activity', () => {
    const root = { isConnected: true };
    assert.equal(isForegroundLiveCard({ root, groupId: 'owner', finished: false }), true);
    assert.equal(isForegroundLiveCard({
        root, groupId: 'owner', finished: false, reviewAnchor: true,
    }), false);
});

test('unkeyed terminal incidents do not clear unrelated live turns', () => {
    // The cleanup guard also requires positive terminal evidence. Keep this
    // source contract resilient to the guard's explicit conjunction while
    // still checking the incident carve-out below it.
    const cleanupStart = chatSource.indexOf('if (!finalizing && (!explicitTaskId || typedTerminal))');
    const finalCleanup = chatSource.slice(
        cleanupStart,
        chatSource.indexOf("if (msg.system_type === 'task_summary')", cleanupStart),
    );
    assert.match(
        finalCleanup,
        /} else if \(msg\.system_type !== 'terminal_incident'\) \{[^}]*?activeDirectActivities\.clear\(\);[^}]*?pendingSubmissions\.clear\(\);\s*}\s*}/,
    );
    // Ordinary unkeyed finals retain their existing global cleanup semantics.
    assert.match(finalCleanup, /activeDirectActivities\.clear\(\);/);
    assert.match(finalCleanup, /pendingSubmissions\.clear\(\);/);
});

test('routing receipts display the event-time label while keeping raw target metadata', () => {
    const annotation = {
        action: 'steer_task',
        status: 'delivered',
        target: 'opaque-task-id',
        target_label: 'Launch 🚀 › Ship release',
    };
    assert.equal(routingAnnotationText(annotation), 'Steered task · Launch 🚀 › Ship release');
    assert.equal(annotation.target, 'opaque-task-id');
});

test('legacy routing receipts and manual choices use neutral labels, never raw ids', () => {
    assert.equal(routingAnnotationText({
        action: 'steer_task', status: 'delivered', target: 'opaque-task-id',
    }), 'Steered task · Task');
    assert.equal(routingAnnotationText({
        action: 'project_route', status: 'delivered', target: 'opaque-project-id',
    }), 'Project routing · Project');
    const manual = routingAnnotationText({
        action: 'manual', status: 'needs_manual_target', options: [
            { action: 'steer_task', task_id: 'opaque-option-id' },
            { action: 'project_route', project_id: 'opaque-project-id' },
        ],
    });
    assert.equal(manual, 'Choose a target · Task / Project');
    assert.equal(manual.includes('opaque-'), false);
});

test('state snapshots and failure authority stay monotonic across reversed completion', () => {
    const applied = [];
    const requestTimes = [100, 200];
    const snapshots = createStateSnapshotSequencer(
        (data, requestedAt) => applied.push({ data, requestedAt }),
        () => requestTimes.shift(),
    );
    const older = snapshots.begin();
    const newer = snapshots.begin();

    assert.deepEqual(older, { generation: 1, requestedAt: 100 });
    assert.deepEqual(newer, { generation: 2, requestedAt: 200 });
    assert.equal(snapshots.isCurrent(older), true);
    assert.equal(snapshots.apply(newer, { activities: [] }), true);
    // An older non-OK/catch path can no longer regress the newer header.
    assert.equal(snapshots.isCurrent(older), false);
    assert.equal(snapshots.apply(older, { activities: ['stale-root'] }), false);
    assert.deepEqual(applied, [{ data: { activities: [] }, requestedAt: 200 }]);
});

test('snapshot provenance beats apply time while equal-time live frames keep the request barrier', () => {
    let activities = new Map();
    const snapshots = createStateSnapshotSequencer((data, requestedAt, generation) => {
        activities = computeHydratedDirectActivities(
            activities, data.activities, 1, requestedAt, null, generation,
        );
    }, () => 100);
    const older = snapshots.begin();
    const newer = snapshots.begin();
    const originalNow = Date.now;
    Date.now = () => 300;
    try {
        assert.equal(snapshots.apply(older, { activities: [
            { activity_id: 'snapshot-root', chat_id: 1, kind: 'managed_task' },
        ] }), true);
        assert.equal(activities.get('snapshot-root').snapshotGeneration, older.generation);
        // A genuinely later live frame can share the request's millisecond.
        activities.set('live-root', {
            activityId: 'live-root', kind: 'managed_task', startedAt: newer.requestedAt,
        });
        assert.equal(snapshots.apply(newer, { activities: [] }), true);
        assert.equal(activities.has('snapshot-root'), false);
        assert.equal(activities.has('live-root'), true);
    } finally {
        Date.now = originalNow;
    }
});

test('computeDerivedChatStatus: offline state when ws is disconnected', () => {
    const status = computeDerivedChatStatus({
        isConnected: false,
        hasActiveLiveCard: true,
        activeDirectCount: 1,
        pendingSubmissionsCount: 1,
    });
    assert.deepEqual(status, {
        kind: 'offline',
        text: 'Reconnecting...',
        showDots: false,
    });
});

test('computeDerivedChatStatus: working state when active live card is present', () => {
    const status = computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: true,
        activeDirectCount: 0,
        pendingSubmissionsCount: 0,
    });
    assert.deepEqual(status, {
        kind: 'thinking',
        text: 'Working...',
        showDots: false,
    });
});

test('computeDerivedChatStatus: thinking state with dots when direct activities are active', () => {
    const status = computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 1,
        pendingSubmissionsCount: 0,
    });
    assert.deepEqual(status, {
        kind: 'thinking',
        text: 'Thinking...',
        showDots: true,
    });
});

test('computeDerivedChatStatus: sending state with dots when local submissions are pending', () => {
    const status = computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 1,
    });
    assert.deepEqual(status, {
        kind: 'thinking',
        text: 'Sending...',
        showDots: true,
    });
});

test('computeDerivedChatStatus: idle server status remains Online', () => {
    const status = computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 0,
    });
    assert.deepEqual(status, {
        kind: 'online',
        text: 'Online',
        showDots: false,
    });
});

test('computeDerivedChatStatus: online idle state by default', () => {
    const status = computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 0,
    });
    assert.deepEqual(status, {
        kind: 'online',
        text: 'Online',
        showDots: false,
    });
});

test('computeHydratedDirectActivities: filters turns by chatId', () => {
    const turns = [
        { activity_id: 'act-main-1', chat_id: 1, kind: 'direct_chat', phase: 'thinking' },
        { activity_id: 'act-proj-2', chat_id: 2, kind: 'direct_chat', phase: 'thinking' },
        { activity_id: 'act-main-2', chat_id: 1, kind: 'direct_chat', phase: 'thinking' },
    ];

    const mapChat1 = computeHydratedDirectActivities(new Map(), turns, 1);
    assert.equal(mapChat1.size, 2);
    assert.ok(mapChat1.has('act-main-1'));
    assert.ok(mapChat1.has('act-main-2'));
    assert.ok(!mapChat1.has('act-proj-2'));

    const mapChat2 = computeHydratedDirectActivities(new Map(), turns, 2);
    assert.equal(mapChat2.size, 1);
    assert.ok(mapChat2.has('act-proj-2'));
});

test('computeHydratedDirectActivities: removes completed activities not in snapshot', () => {
    const initialMap = new Map([
        ['act-old', { activityId: 'act-old', kind: 'direct_chat', phase: 'thinking' }],
        ['act-keep', { activityId: 'act-keep', kind: 'direct_chat', phase: 'thinking' }],
    ]);

    const turns = [
        { activity_id: 'act-keep', chat_id: 1, kind: 'direct_chat', phase: 'working' },
        { activity_id: 'act-new', chat_id: 1, kind: 'direct_chat', phase: 'thinking' },
    ];

    const updated = computeHydratedDirectActivities(initialMap, turns, 1);
    assert.equal(updated.size, 2);
    assert.ok(!updated.has('act-old'));
    assert.ok(updated.has('act-keep'));
    assert.equal(updated.get('act-keep').phase, 'working');
    assert.ok(updated.has('act-new'));
});

test('computeHydratedDirectActivities: a stale snapshot never wipes an activity registered after the snapshot was requested', () => {
    const snapshotRequestedAt = 1_000_000;
    const initialMap = new Map([
        // Registered by a WS typing frame AFTER the /api/state request went out:
        // the (stale) empty snapshot has no authority over it.
        ['act-fresh', { activityId: 'act-fresh', kind: 'direct_chat', phase: 'thinking', startedAt: snapshotRequestedAt + 50 }],
        // Existed before the snapshot was requested and is absent from it: done.
        ['act-stale', { activityId: 'act-stale', kind: 'direct_chat', phase: 'thinking', startedAt: snapshotRequestedAt - 50 }],
    ]);

    const updated = computeHydratedDirectActivities(initialMap, [], 1, snapshotRequestedAt);
    assert.equal(updated.size, 1);
    assert.ok(updated.has('act-fresh'));
    assert.ok(!updated.has('act-stale'));

    // Without a barrier (default Infinity) the snapshot wipes both — legacy behavior.
    const legacy = computeHydratedDirectActivities(initialMap, [], 1);
    assert.equal(legacy.size, 0);
});

test('computeHydratedDirectActivities: startedAt stays client-clock (snapshot server time never enters the barrier)', () => {
    const clientObservedAt = 5_000;
    const initialMap = new Map([
        ['act-1', { activityId: 'act-1', kind: 'direct_chat', phase: 'thinking', startedAt: clientObservedAt }],
    ]);
    // Snapshot lists the same activity with a (skewed) server-clock started_at.
    const turns = [
        { activity_id: 'act-1', chat_id: 1, kind: 'direct_chat', phase: 'working', started_at: 99_999_999 },
    ];
    const updated = computeHydratedDirectActivities(initialMap, turns, 1, 10_000);
    assert.equal(updated.get('act-1').startedAt, clientObservedAt);
    assert.equal(updated.get('act-1').phase, 'working');
});

test('computeHydratedDirectActivities: correctly handles chat_id=0 without coercing to 1', () => {
    const turns = [
        { activity_id: 'act-sys-0', chat_id: 0, kind: 'direct_chat', phase: 'thinking' },
        { activity_id: 'act-main-1', chat_id: 1, kind: 'direct_chat', phase: 'thinking' },
    ];

    const mapChat0 = computeHydratedDirectActivities(new Map(), turns, 0);
    assert.equal(mapChat0.size, 1);
    assert.ok(mapChat0.has('act-sys-0'));

    const mapChat1 = computeHydratedDirectActivities(new Map(), turns, 1);
    assert.equal(mapChat1.size, 1);
    assert.ok(mapChat1.has('act-main-1'));
});

test('computeHydratedDirectActivities: snapshot has no deletion authority over managed-task typing entries (no kind stamp)', () => {
    const initialMap = new Map([
        // Queued managed task's typing frame carries no kind: the direct
        // registry does not track it, so an (empty) snapshot must not end it.
        ['managed-1', { activityId: 'managed-1', kind: '', phase: 'thinking', startedAt: 100 }],
        // Registry-tracked direct turn absent from the snapshot: concluded.
        ['direct-1', { activityId: 'direct-1', kind: 'direct_chat', phase: 'thinking', startedAt: 100 }],
        ['direct-2', { activityId: 'direct-2', kind: 'direct_chat', phase: 'thinking', startedAt: 100 }],
    ]);

    const updated = computeHydratedDirectActivities(initialMap, [], 1, 1_000);
    assert.equal(updated.size, 1);
    assert.ok(updated.has('managed-1'));
    assert.ok(!updated.has('direct-1'));
    assert.ok(!updated.has('direct-2'));
});

test('computeHydratedDirectActivities: a concluded turn is never resurrected by a snapshot captured while it still ran', () => {
    const concluded = new Map([['act-done', 12345]]);
    // Snapshot was requested mid-turn, its response arrived AFTER the turn's
    // keyed final concluded the activity client-side (project panels hydrate
    // once at creation, so without the ledger this insert would be permanent).
    const turns = [
        { activity_id: 'act-done', chat_id: 1, kind: 'direct_chat', phase: 'thinking' },
        { activity_id: 'act-live', chat_id: 1, kind: 'direct_chat', phase: 'thinking' },
    ];
    const updated = computeHydratedDirectActivities(new Map(), turns, 1, Infinity, concluded);
    assert.equal(updated.size, 1);
    assert.ok(!updated.has('act-done'));
    assert.ok(updated.has('act-live'));
});

test('computeDerivedChatStatus: priority order is preserved (offline > live card > direct thinking > sending > online)', () => {
    // 1. Disconnected beats everything
    assert.equal(computeDerivedChatStatus({
        isConnected: false,
        hasActiveLiveCard: true,
        activeDirectCount: 5,
        pendingSubmissionsCount: 3,
    }).text, 'Reconnecting...');

    // 2. Active live card beats direct thinking & sending & attention
    assert.equal(computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: true,
        activeDirectCount: 5,
        pendingSubmissionsCount: 3,
    }).text, 'Working...');

    // 3. Direct thinking beats local pending submissions & attention
    assert.equal(computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 2,
        pendingSubmissionsCount: 3,
    }).text, 'Thinking...');

    // 4. Local pending submissions beat idle
    assert.equal(computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 1,
    }).text, 'Sending...');

    // 5. No server activity means Online
    assert.equal(computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 0,
    }).text, 'Online');

    // 6. Clean idle state
    assert.equal(computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 0,
    }).text, 'Online');
});

test('computeDerivedChatStatus: budget-paused work is not Working or Queued (#322)', () => {
    assert.deepEqual(
        computeDerivedChatStatus({ pausedManagedCount: 2 }),
        { kind: 'online', text: 'Paused (budget)', showDots: false },
    );
    // Runnable queued work outranks the paused disclosure.
    assert.equal(
        computeDerivedChatStatus({ pausedManagedCount: 1, queuedManagedCount: 1 }).text,
        'Queued...',
    );
    // Active work outranks both.
    assert.equal(
        computeDerivedChatStatus({ pausedManagedCount: 1, activeManagedCount: 1 }).text,
        'Working...',
    );
});
