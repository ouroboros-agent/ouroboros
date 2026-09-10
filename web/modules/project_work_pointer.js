/** A navigation pointer to existing Project cards, never another work dashboard. */
export function projectWorkTarget(records = []) {
    const roots = records.filter(record => !record.isSubagent && record.root?.isConnected);
    return roots.filter(record => !record.finished).at(-1) || roots.at(-1) || null;
}

export function bindProjectWorkPointer(host, { records, getWindow, onNavigate }) {
    const button = host.ownerDocument.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-ghost project-work-pointer';
    const note = host.ownerDocument.createElement('span');
    note.className = 'project-work-coverage';
    let target = null;
    let disposed = false;
    const navigate = () => {
        update();
        if (target?.root?.isConnected) onNavigate(target.root);
    };
    function update() {
        if (disposed) return;
        target = projectWorkTarget([...records.values()]);
        const label = target
            ? `${target.finished ? 'Latest task' : 'Working'} · ${target.titleEl?.textContent || 'Task'}`
            : 'No task card in loaded messages';
        if (button.textContent !== label) button.textContent = label;
        button.disabled = !target;
        // A represented card is not a claim that all Project work/history is loaded.
        const coverage = getWindow()?.complete === true ? '' : 'Loaded messages only';
        if (note.textContent !== coverage) note.textContent = coverage;
        note.hidden = !coverage;
    }
    button.addEventListener('click', navigate);
    host.prepend(button, note);
    update();
    return {
        update,
        destroy() {
            disposed = true;
            button.removeEventListener('click', navigate);
            button.remove();
            note.remove();
            target = null;
        },
    };
}
