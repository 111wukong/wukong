/* Pure helpers for the task fan-out.
 *
 * Kept out of sidebar.js so the aggregation is testable without a DOM: it is
 * arithmetic over rows, not rendering.
 */

/** Sum each task with its descendants in `rows`, keyed by task id.
 *
 * A parent's own budget hides the workers it spawned: "fan out to five" shows
 * the orchestrator's few steps and not the fifteen the children spent. This
 * returns, for every task, the subtree totals (tasks / steps / tokens / cost)
 * so a parent row can show what the whole fan-out cost.
 *
 * Only rows present in the current view count. The caller passes exactly what
 * it is rendering, so a filtered or paginated list never claims a total it
 * cannot see. A child whose parent is absent is treated as a root, matching
 * the flat indented list in the sidebar.
 */
export function subtreeTotals(rows) {
  const byId = new Map(rows.map((task) => [task.id, task]));
  const kids = new Map();
  for (const task of rows) {
    const parent = task.parent_task_id;
    if (parent && byId.has(parent)) {
      if (!kids.has(parent)) kids.set(parent, []);
      kids.get(parent).push(task);
    }
  }

  const memo = new Map();
  const sum = (task) => {
    if (memo.has(task.id)) return memo.get(task.id);
    const acc = {
      tasks: 1,
      steps: task.steps_used || 0,
      tokens: (task.tokens_in || 0) + (task.tokens_out || 0),
      cost: task.cost_usd || 0,
    };
    for (const child of kids.get(task.id) || []) {
      const below = sum(child);
      acc.tasks += below.tasks;
      acc.steps += below.steps;
      acc.tokens += below.tokens;
      acc.cost += below.cost;
    }
    memo.set(task.id, acc);
    return acc;
  };

  for (const task of rows) sum(task);
  return memo;
}
