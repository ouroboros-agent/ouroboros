// Owner decision cards: the typed quiz card (question + option buttons +
// stake + assumption) and the routing picker (#198) — one decision-card
// family, one answer contract (POST /api/decisions). Optional questions let
// the task keep working under an assumption; required questions wait for an
// answer. Both read as a record after settlement. The routing picker
// settles into the plain routing ack line once its dispatch is confirmed.
import { MAX_DECISION_COMMENT, MAX_QUIZ_OPTIONS } from './api_types.js';
import { renderRoutingAnnotation, routingOptionLabel } from './chat_activity.js';
import { createSystemMessageAction } from './ui_helpers.js';

const QUIZ_STATUS_TEXT = {
    open: 'Awaiting answer',
    answered: 'Answered',
    expired_terminal: 'Task finished — question expired',
    superseded: 'Superseded by a retry',
};

// Neutral, factual statuses (owner decision 15~A): the card never scolds the
// router — it states what the click does and what happened.
const ROUTING_STATUS_TEXT = {
    open: 'Choose a destination',
    pending: 'Routing…',
    answered: 'Routed',
    superseded: 'Superseded by a newer attempt',
};
const ROUTING_TOP_OPTIONS = 8;

export function createChatDecision({
    apiFetch,
    frameNode,
    renderMarkdown,
    enhanceMarkdown,
    showToast,
    fetchDetail = null,
    onDomWrite = (mutate) => mutate(),
    isMain = false,
    insertMessageNode = null,
}) {
    const observations = new Map();
    const quizViews = new Map();
    const pointerViews = new Map();
    const detailReads = new Map();
    const questionKey = (taskId, quizId) => JSON.stringify([String(taskId || ''), String(quizId || '')]);
    let disposed = false;
    let questionNavigation = 0;
    function observe(frame) {
        const key = questionKey(frame.task_id, frame.quiz_id);
        const previous = observations.get(key);
        if (!frame.task_id || !frame.quiz_id) return frame;
        if (previous && previous.state !== 'open' && frame.state === 'open') return previous;
        if (previous?.state === 'answered' && frame.state === 'expired_terminal') return previous;
        if (['open', 'answered', 'expired_terminal', 'superseded'].includes(frame.state)) {
            observations.set(key, { ...previous, ...frame });
            if (observations.size > 2000) observations.delete(observations.keys().next().value);
        }
        return ['answered', 'expired_terminal', 'superseded'].includes(observations.get(key)?.state)
            ? observations.get(key) : frame;
    }

    async function readQuestion(taskId, quizId, projectId) {
        if (!fetchDetail || disposed) return null;
        if (!detailReads.has(taskId)) {
            const promise = Promise.resolve().then(() => fetchDetail(taskId))
                .finally(() => { if (detailReads.get(taskId) === promise) detailReads.delete(taskId); });
            detailReads.set(taskId, promise);
        }
        const detail = await detailReads.get(taskId);
        const block = detail?.owner_quiz?.[quizId];
        if (disposed || String(detail?.task_id || detail?.id || '') !== String(taskId)
            || (projectId && String(detail?.project_id || '') !== String(projectId))
            || !block || String(block.quiz_id || '') !== String(quizId)
            || !['open', 'answered', 'expired_terminal', 'superseded'].includes(block.state)) return null;
        const current = observe({ ...block, task_id: taskId });
        return { ...block, ...current, task_id: taskId, project_id: detail.project_id,
            ts: block.asked_at, owner_wait_state: detail.owner_wait?.quiz_id === quizId ? detail.owner_wait.state : '' };
    }

    async function revealQuestion(taskId, quizId, projectId, chatId, appendQuiz, isVisible, beforeReveal = () => {}) {
        const navigation = ++questionNavigation;
        const current = () => !disposed && isVisible() && navigation === questionNavigation;
        if (!projectId || !taskId || !quizId || !current()) return false;
        let card = quizViews.get(questionKey(taskId, quizId));
        if (!card) {
            try {
                const question = await readQuestion(taskId, quizId, projectId);
                if (!current()) return false;
                if (!question) { showToast('Question unavailable.', 'error'); return false; }
                onDomWrite(() => appendQuiz({ ...question, chat_id: chatId, type: 'quiz' }));
                card = quizViews.get(questionKey(taskId, quizId));
            } catch {
                if (current()) showToast('Question unavailable.', 'error');
                return false;
            }
        }
        if (!current() || !card) return false;
        // An explicit target supersedes any pending restoration of the room's
        // earlier scroll position; the chat instance owns that restoration.
        beforeReveal();
        card.scrollIntoView?.({ block: 'center', behavior: 'auto' });
        (card.querySelector('.chat-quiz-comment') || card.querySelector('.chat-quiz-question'))?.focus?.({ preventScroll: true });
        return true;
    }

    function pointerText(row, state) {
        const lead = state === 'answered' ? 'Question answered'
            : ['expired_terminal', 'superseded'].includes(state) ? 'Question expired'
                : state !== 'open' ? 'Question status unavailable'
                    : row.owner_wait_state === 'resumed' ? 'Question' : 'Answer needed';
        return `${lead} in ${row.project_name || 'Project'}`;
    }

    function updatePointer(view, frame) {
        const current = observe({ ...frame, state: frame.quiz_state || frame.state });
        view.row = { ...view.row, ...frame };
        const state = String(current.state || 'unknown');
        return onDomWrite(() => {
            const text = pointerText(view.row, state);
            const action = state === 'open' ? 'Open question' : 'View question';
            const changed = view.label.textContent !== text || view.action.textContent !== action
                || view.card.dataset.state !== state;
            if (view.label.textContent !== text) view.label.textContent = text;
            if (view.card.dataset.state !== state) view.card.dataset.state = state;
            if (view.action.textContent !== action) view.action.textContent = action;
            return changed;
        });
    }

    async function refreshPointer(view) {
        const { task_id: taskId, quiz_id: quizId, project_id: projectId } = view.row;
        try {
            const question = await readQuestion(taskId, quizId, projectId);
            if (!disposed && pointerViews.get(questionKey(taskId, quizId)) === view) {
                updatePointer(view, { ...view.row, quiz_state: question?.state || 'unknown',
                    owner_wait_state: question?.owner_wait_state || '' });
            }
        } catch {
            if (!disposed && pointerViews.get(questionKey(taskId, quizId)) === view)
                updatePointer(view, { ...view.row, quiz_state: 'unknown' });
        }
    }

    function buildQuestionPointer(msg) {
        if (!msg.task_id || !msg.quiz_id || !msg.project_id || !msg.project_chat_id) return null;
        const key = questionKey(msg.task_id, msg.quiz_id);
        const prior = pointerViews.get(key);
        if (prior) { updatePointer(prior, msg); return null; }
        const card = document.createElement('div');
        card.className = 'project-question-pointer';
        card.dataset.taskId = String(msg.task_id);
        card.dataset.quizId = String(msg.quiz_id);
        const label = document.createElement('span');
        const view = { row: { ...msg }, card, label, action: null };
        view.action = createSystemMessageAction({ label: 'Open question', onClick: () => {
            window.dispatchEvent(new CustomEvent('ouro:open-project', { detail: {
                project: { id: view.row.project_id, name: view.row.project_name, chat_id: view.row.project_chat_id },
                task_id: view.row.task_id, quiz_id: view.row.quiz_id,
            } }));
        } });
        card.append(label, view.action);
        const bubble = frameNode(msg, card);
        bubble.classList.remove('assistant');
        bubble.classList.add('system');
        const sender = bubble.querySelector('.sender');
        if (sender) sender.textContent = 'System';
        pointerViews.set(key, view);
        // Initial open delivery can race a closed frame missed by this instance.
        const state = observations.get(key)?.state || (msg.quiz_state === 'open' && fetchDetail ? 'unknown' : msg.quiz_state);
        updatePointer(view, { ...msg, quiz_state: state });
        if (fetchDetail && !['answered', 'expired_terminal', 'superseded'].includes(state)) void refreshPointer(view);
        return bubble;
    }

    function appendQuestionPointer(msg) {
        if (!isMain || !insertMessageNode) return false;
        return onDomWrite(() => {
            const bubble = buildQuestionPointer(msg);
            return bubble ? insertMessageNode(bubble) !== false : false;
        });
    }

    function normalizeQuiz(msg) {
        const nested = msg && typeof msg.quiz === 'object' && msg.quiz ? msg.quiz : null;
        const src = nested || msg || {};
        // Strict per-card validation: ONE corrupt option refuses THIS card
        // (buildQuizCard -> null), never the whole history hydration pass.
        // Filtering instead would silently shift option_index against the
        // producer's original list — a wrong answer, not a degraded card.
        const raw = Array.isArray(src.options) ? src.options : [];
        const normalized = raw.map((option, index) => (typeof option === 'string'
            ? { label: option, ...(src.option_details?.[index] ? { detail: src.option_details[index] } : {}) } : option));
        const corrupt = normalized.some(
            (option) => !option || typeof option !== 'object' || !String(option.label || '').trim());
        const options = corrupt ? [] : normalized.slice(0, MAX_QUIZ_OPTIONS);
        return {
            quizId: String(src.quiz_id || ''),
            question: String((nested ? msg.text : src.question) || ''),
            options,
            stake: String(src.stake || ''),
            assumption: String(src.assumption || ''),
            waitForAnswer: src.wait_for_answer === true,
            state: String(src.state || 'open'),
            taskId: String(msg.task_id || ''),
            ts: msg.ts || null,
            answeredIndex: Number.isInteger(src.answered_index) ? src.answered_index : null,
            // The owner's verbatim words on a settled card (history replay
            // merges them from the projection). With no answeredIndex they
            // ARE the answer, not a remark beside one.
            comment: String(src.comment || ''),
            detailsUnavailable: src.option_details === undefined && raw.every((option) => typeof option === 'string'),
        };
    }

    function statusText(state) {
        // Unknown states read as settled, never as an open invitation.
        return QUIZ_STATUS_TEXT[state] || 'Closed';
    }

    async function submitAnswer(card, quiz, index, comment) {
        if (card.dataset.pending === '1') return;
        card.dataset.pending = '1';
        const text = String(comment || '');
        // STABLE per-card idempotency key: a retry after a transient failure
        // must replay the SAME request, or the server-side first-wins latch
        // reads the retry as a competing second answer.
        if (!card.dataset.requestId) {
            card.dataset.requestId = (crypto.randomUUID && crypto.randomUUID()) || `q-${Date.now()}`;
        }
        try {
            const res = await apiFetch('/api/decisions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    request_id: card.dataset.requestId,
                    decision_id: `quiz:${quiz.taskId}:${quiz.quizId}`,
                    // Omitted, never null: no option means the owner took none
                    // of them and the comment carries the whole answer.
                    ...(Number.isInteger(index) ? { option_index: index } : {}),
                    ...(text ? { comment: text } : {}),
                }),
            });
            let body = null;
            try { body = res && res.json ? await res.json() : null; } catch (parseErr) { body = null; }
            if (res && res.ok) {
                // The confirmation is the display truth: a same-request_id
                // retry may have carried a different payload, and the server
                // answers with what was actually RECORDED — index absent for a
                // free answer, comment as stored. Never render this attempt's
                // own click over it.
                const answered = body && Number.isInteger(body.answered_index)
                    ? body.answered_index
                    : (body && body.duplicate ? null : (Number.isInteger(index) ? index : null));
                const recorded = body && typeof body.comment === 'string' ? body.comment : text;
                if (recorded) card.dataset.ownerComment = recorded;
                else delete card.dataset.ownerComment;
                setCardState(card, 'answered', answered);
                return;
            }
            const status = res ? res.status : 0;
            if (status === 409 && body && body.state) {
                // The refusal body carries the card's TRUE lifecycle state —
                // an already-answered quiz settles as answered (with the
                // winning option when known), never as a false expiry.
                const answered = Number.isInteger(body.answered_index) ? body.answered_index : null;
                // The 409 loser learns the WINNING answer, comment included —
                // the local draft must not survive as the displayed record.
                if (typeof body.comment === 'string' && body.comment) card.dataset.ownerComment = body.comment;
                else delete card.dataset.ownerComment;
                setCardState(card, body.state, answered);
                showToast(body.state === 'answered'
                    ? 'Already answered.' : 'This question is no longer open.', 'error');
                return;
            }
            if (status === 409 && card.dataset.state === 'open') {
                setCardState(card, 'expired_terminal', null);
                showToast('This question is no longer open.', 'error');
                return;
            }
            showToast(`Could not record the answer (${status || 'network error'}).`, 'error');
        } catch (err) {
            showToast('Could not record the answer (network error).', 'error');
        } finally {
            delete card.dataset.pending;
        }
    }

    function renderOwnerAnswer(card, comment) {
        // The owner's own words are a SECOND primary line under the question:
        // with no chosen option they are the entire answer, and beside a
        // chosen one they qualify it.
        let line = card.querySelector('.chat-quiz-answer');
        if (!comment) {
            if (!line) return false;
            line.remove();
            return true;
        }
        const text = `Owner's answer: ${comment}`;
        if (line) {
            if (line.textContent === text) return false;
            line.textContent = text;
            return true;
        }
        line = document.createElement('div');
        line.className = 'chat-quiz-answer';
        line.textContent = text;
        const assumption = card.querySelector('.chat-quiz-assumption');
        if (assumption) assumption.before(line);
        else card.append(line);
        return true;
    }

    function setCardState(card, state, answeredIndex) {
        if (!card) return false;
        const current = observe({ task_id: card.dataset.taskId, quiz_id: card.dataset.quizId,
            state, answered_index: answeredIndex, comment: card.dataset.ownerComment || '' });
        state = current.state;
        answeredIndex = Number.isInteger(current.answered_index) ? current.answered_index : null;
        return onDomWrite(() => {
            let changed = card.dataset.state !== state;
            if (changed) card.dataset.state = state;
            if (state !== 'open') {
                // A settled card takes no more input: the draft field goes,
                // and what the owner actually said takes its place.
                const box = card.querySelector('.chat-quiz-comment-box');
                if (box) { box.remove(); changed = true; }
                if (renderOwnerAnswer(card, String(card.dataset.ownerComment || ''))) changed = true;
                const waiting = card.querySelector('.chat-quiz-wait');
                if (waiting) { waiting.remove(); changed = true; }
            }
            const status = card.querySelector('.chat-quiz-status-text');
            const nextStatus = statusText(state);
            if (status && status.textContent !== nextStatus) {
                status.textContent = nextStatus;
                changed = true;
            }
            const buttons = card.querySelectorAll('.chat-quiz-option');
            buttons.forEach((btn, i) => {
                const disabled = state !== 'open';
                const chosen = answeredIndex !== null && i === answeredIndex;
                if (btn.disabled !== disabled) {
                    btn.disabled = disabled;
                    changed = true;
                }
                if (btn.classList.contains('chosen') !== chosen) {
                    btn.classList.toggle('chosen', chosen);
                    changed = true;
                }
            });
            return changed;
        });
    }

    function buildQuizCard(msg) {
        const quiz = normalizeQuiz(msg);
        if (!quiz.quizId || !quiz.taskId || !quiz.question || quiz.options.length < 2) return null;
        const key = questionKey(quiz.taskId, quiz.quizId);
        const current = observe({ task_id: quiz.taskId, quiz_id: quiz.quizId, state: quiz.state,
            answered_index: quiz.answeredIndex, comment: quiz.comment });
        quiz.state = current.state;
        quiz.answeredIndex = Number.isInteger(current.answered_index) ? current.answered_index : null;
        quiz.comment = current.comment || '';
        const existing = quizViews.get(key);
        if (existing) {
            if (quiz.comment) existing.dataset.ownerComment = quiz.comment;
            if (!quiz.detailsUnavailable) {
                existing.querySelectorAll('.chat-quiz-option').forEach((button, index) => {
                    const detail = quiz.options[index]?.detail;
                    if (detail && !button.querySelector('.chat-quiz-option-detail')) {
                        const line = document.createElement('span');
                        line.className = 'chat-quiz-option-detail'; line.textContent = detail; button.append(line);
                    }
                });
                existing.querySelector('.chat-quiz-details-unavailable')?.remove();
            }
            setCardState(existing, quiz.state, quiz.answeredIndex);
            return null;
        }

        const card = document.createElement('div');
        card.className = 'chat-quiz-card';
        card.dataset.quizId = quiz.quizId;
        card.dataset.taskId = quiz.taskId;
        quizViews.set(key, card);

        const head = document.createElement('div');
        head.className = 'chat-quiz-head';
        const chip = document.createElement('span');
        chip.className = 'chat-quiz-chip';
        chip.textContent = 'Question';
        const status = document.createElement('span');
        status.className = 'chat-quiz-status';
        const dot = document.createElement('span');
        dot.className = 'chat-quiz-dot';
        const statusLabel = document.createElement('span');
        statusLabel.className = 'chat-quiz-status-text';
        status.append(dot, statusLabel);
        head.append(chip, status);
        card.append(head);

        // DRY with the chat surface (owner requirement): question and stake go
        // through the SAME sanitizing markdown pipeline as assistant bubbles,
        // so chat rendering improvements reach the card automatically.
        const question = document.createElement('div');
        question.className = 'chat-quiz-question';
        question.tabIndex = -1;
        if (renderMarkdown) question.innerHTML = renderMarkdown(quiz.question);
        else question.textContent = quiz.question;
        card.append(question);

        if (quiz.stake) {
            const stake = document.createElement('div');
            stake.className = 'chat-quiz-stake';
            if (renderMarkdown) stake.innerHTML = renderMarkdown(`At stake: ${quiz.stake}`);
            else stake.textContent = `At stake: ${quiz.stake}`;
            card.append(stake);
        }

        let commentField = null;
        // The raw field value is the answer (VERBATIM to the model); the
        // trimmed view only decides whether there IS one.
        const commentText = () => String((commentField && commentField.value) || '');
        const commentPresent = () => commentText().trim().length > 0;

        const optionsBox = document.createElement('div');
        optionsBox.className = 'chat-quiz-options';
        quiz.options.forEach((option, index) => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'chat-quiz-option';
            const label = document.createElement('span');
            label.className = 'chat-quiz-option-label';
            label.textContent = String(option.label || '');
            btn.append(label);
            const detailText = String(option.detail || '');
            if (detailText) {
                const detail = document.createElement('span');
                detail.className = 'chat-quiz-option-detail';
                detail.textContent = detailText;
                btn.append(detail);
            }
            btn.addEventListener('click', () => {
                if (card.dataset.state !== 'open') return;
                // A typed remark rides WITH the click: the owner picked this
                // option and said why, one answer, one request.
                submitAnswer(card, quiz, index, commentText());
            });
            optionsBox.append(btn);
        });
        card.append(optionsBox);
        if (quiz.detailsUnavailable) {
            const note = document.createElement('div');
            note.className = 'chat-quiz-stake chat-quiz-details-unavailable';
            note.textContent = 'Option details were not retained for this older question.';
            card.append(note);
        }

        // Free answer: none of the options may fit, and the owner must not be
        // forced to pick the least wrong one. Always visible while the card is
        // open (no disclosure to discover), removed once it settles.
        if (quiz.state === 'open') {
            const box = document.createElement('div');
            box.className = 'chat-quiz-comment-box';
            commentField = document.createElement('textarea');
            commentField.className = 'chat-quiz-comment';
            commentField.rows = 2;
            commentField.maxLength = MAX_DECISION_COMMENT;
            commentField.placeholder = 'Your answer or comment…';
            const send = document.createElement('button');
            send.type = 'button';
            send.className = 'chat-quiz-send';
            send.textContent = 'Send my answer';
            send.disabled = true;
            const syncSend = () => {
                const text = commentText();
                const enabled = commentPresent() && text.length <= MAX_DECISION_COMMENT;
                if (send.disabled === !enabled) return;
                send.disabled = !enabled;
            };
            commentField.addEventListener('input', () => onDomWrite(() => { syncSend(); return true; }));
            send.addEventListener('click', () => {
                if (card.dataset.state !== 'open') return;
                const text = commentText();
                if (!commentPresent()) return;
                if (text.length > MAX_DECISION_COMMENT) {
                    // The ingress refuses it rather than truncating the
                    // owner's words — say so here instead of sending.
                    showToast(`Keep the answer under ${MAX_DECISION_COMMENT} characters — `
                        + 'it is delivered word for word.', 'error');
                    return;
                }
                submitAnswer(card, quiz, null, text);
            });
            box.append(commentField, send);
            card.append(box);
        }

        // The signature line: what the agent keeps doing while the owner has
        // not answered — and, once the card settles, the record of the path
        // it took by default.
        if (quiz.assumption || quiz.waitForAnswer) {
            const assumption = document.createElement('div');
            assumption.className = 'chat-quiz-assumption';
            if (quiz.waitForAnswer) assumption.classList.add('chat-quiz-wait');
            assumption.textContent = quiz.waitForAnswer
                ? 'Waiting for your answer; Stop and the task deadline still apply.'
                : `Continuing meanwhile: ${quiz.assumption}`;
            card.append(assumption);
        }

        if (quiz.comment) card.dataset.ownerComment = quiz.comment;
        setCardState(card, quiz.state, quiz.answeredIndex);
        const framed = frameNode(msg, card);
        if (enhanceMarkdown && renderMarkdown) enhanceMarkdown(card);
        return framed;
    }

    function setRoutingCardState(card, state, chosenIndex) {
        if (!card) return false;
        return onDomWrite(() => {
            let changed = card.dataset.state !== state;
            if (changed) card.dataset.state = state;
            const status = card.querySelector('.chat-quiz-status-text');
            const nextStatus = ROUTING_STATUS_TEXT[state] || 'Closed';
            if (status && status.textContent !== nextStatus) {
                status.textContent = nextStatus;
                changed = true;
            }
            card.querySelectorAll('.chat-quiz-option').forEach((btn, i) => {
                const disabled = state !== 'open';
                const chosen = chosenIndex !== null && i === chosenIndex;
                if (btn.disabled !== disabled) {
                    btn.disabled = disabled;
                    changed = true;
                }
                if (btn.classList.contains('chosen') !== chosen) {
                    btn.classList.toggle('chosen', chosen);
                    changed = true;
                }
            });
            return changed;
        });
    }

    async function submitRouting(card, cmid, token, index) {
        if (card.dataset.pending === '1') return;
        card.dataset.pending = '1';
        // Same idempotency discipline as the quiz card: ONE stable id per
        // card, replayed on retry, so the server latch never reads a retry
        // as a competing second click.
        if (!card.dataset.requestId) {
            card.dataset.requestId = (crypto.randomUUID && crypto.randomUUID()) || `r-${Date.now()}`;
        }
        try {
            const res = await apiFetch('/api/decisions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    request_id: card.dataset.requestId,
                    decision_id: `routing:${cmid}:${token}`,
                    option_index: index,
                }),
            });
            let body = null;
            try { body = res && res.json ? await res.json() : null; } catch (parseErr) { body = null; }
            if (res && res.ok) {
                const answered = body && Number.isInteger(body.answered_index) ? body.answered_index : index;
                setRoutingCardState(card, 'answered', answered);
                return;
            }
            const status = res ? res.status : 0;
            if (status === 409 && body && body.state) {
                // Honest settlement: the body carries the TRUE state (another
                // click won, or a newer routing attempt superseded this card).
                setRoutingCardState(card,
                    body.state === 'open' ? 'open' : body.state,
                    Number.isInteger(body.answered_index) ? body.answered_index : null);
                showToast(body.state === 'open'
                    ? `Not routed: ${body.reason || 'the destination refused this message'} — pick again.`
                    : body.state === 'pending'
                        ? 'Another choice is already being routed.'
                        : 'This message was already routed.', 'error');
                return;
            }
            showToast(`Could not route the message (${status || 'network error'}) — try again.`, 'error');
        } catch (err) {
            showToast('Could not route the message (network error) — try again.', 'error');
        } finally {
            delete card.dataset.pending;
        }
    }

    function buildRoutingCard(cmid, token, options) {
        const card = document.createElement('div');
        card.className = 'chat-quiz-card chat-routing-card';
        card.dataset.routingToken = token;

        const head = document.createElement('div');
        head.className = 'chat-quiz-head';
        const chip = document.createElement('span');
        chip.className = 'chat-quiz-chip';
        chip.textContent = 'Route';
        const status = document.createElement('span');
        status.className = 'chat-quiz-status';
        const dot = document.createElement('span');
        dot.className = 'chat-quiz-dot';
        const statusLabel = document.createElement('span');
        statusLabel.className = 'chat-quiz-status-text';
        status.append(dot, statusLabel);
        head.append(chip, status);
        card.append(head);

        const optionsBox = document.createElement('div');
        optionsBox.className = 'chat-quiz-options';
        const overflow = options.length > ROUTING_TOP_OPTIONS;
        options.forEach((option, index) => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'chat-quiz-option';
            if (overflow && index >= ROUTING_TOP_OPTIONS) btn.hidden = true;
            const label = document.createElement('span');
            label.className = 'chat-quiz-option-label';
            label.textContent = routingOptionLabel(option) || `Option ${index + 1}`;
            btn.append(label);
            btn.addEventListener('click', () => {
                if (card.dataset.state !== 'open') return;
                submitRouting(card, cmid, token, index);
            });
            optionsBox.append(btn);
        });
        card.append(optionsBox);
        if (overflow) {
            const more = document.createElement('button');
            more.type = 'button';
            more.className = 'chat-quiz-more';
            more.textContent = `Show all ${options.length}`;
            more.addEventListener('click', () => onDomWrite(() => {
                optionsBox.querySelectorAll('.chat-quiz-option')
                    .forEach((btn) => { btn.hidden = false; });
                more.remove();
                return true;
            }));
            card.append(more);
        }
        setRoutingCardState(card, 'open', null);
        return card;
    }

    function renderRoutingDecision(bubble, annotation) {
        // ONE entry point for a user bubble's routing surface: an actionable
        // refusal renders the picker card; every other annotation state
        // settles back into the plain text ack line.
        if (!bubble) return false;
        return onDomWrite(() => {
            const cmid = String(bubble.dataset.clientMessageId || '');
            const status = String((annotation && annotation.status) || '');
            const token = String((annotation && annotation.routing_token) || '');
            const options = Array.isArray(annotation && annotation.options) ? annotation.options : [];
            const actionable = status === 'needs_manual_target' && cmid && token
                && options.length > 0 && options.every((o) => o && typeof o === 'object');
            if (!actionable) {
                const card = bubble.querySelector('.chat-routing-card');
                card?.remove();
                return renderRoutingAnnotation(bubble, annotation) || Boolean(card);
            }
            const annotationChanged = bubble.querySelector('.msg-routing-annotation')
                ? renderRoutingAnnotation(bubble, null) : false;
            let card = bubble.querySelector('.chat-routing-card');
            if (card && card.dataset.routingToken === token) return annotationChanged;
            card?.remove();
            card = buildRoutingCard(cmid, token, options);
            const time = bubble.querySelector('.msg-time');
            if (time) time.before(card);
            else bubble.append(card);
            bubble.dataset.chatAnnotationStatus = status;
            return true;
        });
    }

    function applyQuizStateFrame(rootNode, frame) {
        // Live lifecycle update for an already-rendered card (WS "quiz_state").
        // The card is found by identity, never appended: state changes must
        // not create a second card (the quiz frame dedupe is id+ts keyed).
        const quizId = String(frame && frame.quiz_id || '');
        const taskId = String(frame && frame.task_id || '');
        if (!quizId || !taskId || !rootNode) return false;
        frame = observe(frame);
        const key = questionKey(taskId, quizId);
        const pointer = pointerViews.get(key);
        const changed = pointer ? updatePointer(pointer, frame) : false;
        const card = quizViews.get(key);
        if (!card) return changed;
        const index = Number.isInteger(frame.answered_index) ? frame.answered_index : null;
        // The owner's recorded free-text answer rides the frame (#471) so the
        // live card shows `Owner's answer:` exactly as the replayed card does.
        // Set only when present, never cleared by its absence: a later
        // lifecycle frame (expired/superseded) carries no comment.
        const comment = String(frame.comment || '');
        if (comment) card.dataset.ownerComment = comment;
        return setCardState(card, String(frame.state || ''), index) || changed;
    }

    return { buildQuizCard, buildQuestionPointer, appendQuestionPointer, readQuestion, revealQuestion, setCardState, applyQuizStateFrame, renderRoutingDecision,
        refreshQuestions: () => Promise.all([...pointerViews.values()]
            .filter((view) => !['answered', 'expired_terminal', 'superseded'].includes(view.card.dataset.state))
            .map(refreshPointer)),
        resetViews(rows = []) {
            const keep = new Set(rows.map((row) => questionKey(row.task_id, row.quiz_id || row.quiz?.quiz_id)));
            for (const key of observations.keys()) if (!keep.has(key)) observations.delete(key);
            quizViews.clear(); pointerViews.clear();
        },
        destroy() { disposed = true; observations.clear(); quizViews.clear(); pointerViews.clear(); detailReads.clear(); },
    };
}
