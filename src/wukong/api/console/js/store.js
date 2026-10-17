/* The client's shared state, and a one-line event bus.
 *
 * Modules read `state` directly (it is a plain object and there is exactly one
 * of it) but never write each other's DOM. When one module needs another to
 * react, it emits; `app.js` wires the listeners. Without that rule, the
 * transcript would end up calling into the sidebar, which would call back into
 * the composer, and the import graph would be a cycle.
 */

export const state = {
  threadId: `console-${Math.random().toString(36).slice(2, 8)}`,
  taskId: null,
  sessionId: null,
  //: Render targets for the run currently streaming, reset on RUN_STARTED.
  run: null,
  //: Tasks running anywhere in this process, from /health. The tab's own run
  //: is tracked locally; this catches the ones it did not start -- the agent
  //: is a single in-process object, so a run started from the CLI holds the
  //: workspace too.
  runningTasks: 0,
  models: [],
  defaultModel: "mock",
  _running: false,
  get running() {
    return this._running;
  },
  /* "Is a run going" is a property, not something to remember at each of the
   * six places that change it. Binding it here means a new exit path cannot
   * leave the button saying 发送 while a run streams. */
  set running(value) {
    this._running = value;
    emit("running", value);
  },
};

const bus = new EventTarget();

export function emit(name, detail) {
  bus.dispatchEvent(new CustomEvent(name, { detail }));
}

export function on(name, handler) {
  bus.addEventListener(name, (event) => handler(event.detail));
}

export const $ = (id) => document.getElementById(id);

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* Write only when the value actually changed.
 *
 * Assigning `textContent` replaces the text node even when the string is
 * identical, which is still a mutation: the browser drops and rebuilds the
 * node. Polling every five seconds meant doing that to every row forever.
 */
export function setText(node, value) {
  if (node && node.textContent !== value) node.textContent = value;
}

export function setClass(node, value) {
  if (node && node.className !== value) node.className = value;
}
