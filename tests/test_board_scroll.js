const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "../static/js/board.js"), "utf8");

function boardDrag(initialScroll, pointerX) {
  const handlers = {};
  const frames = new Map();
  const classes = () => {
    const values = new Set();
    return {
      add: (name) => values.add(name),
      remove: (name) => values.delete(name),
      contains: (name) => values.has(name),
    };
  };
  const sourceColumn = {};
  const targetColumn = {
    classList: classes(),
    closest: (selector) => selector === ".board-col" ? targetColumn : null,
  };
  const card = {
    classList: classes(),
    closest: (selector) => selector === ".board-card" ? card :
      selector === ".board-col" ? sourceColumn : null,
  };
  const board = {
    scrollLeft: initialScroll,
    addEventListener: (name, handler) => { handlers[name] = handler; },
    getBoundingClientRect: () => ({ left: 0, right: 400, top: 0, bottom: 200 }),
    querySelector: () => targetColumn.classList.contains("is-drop-target") ? targetColumn : null,
  };
  let frameId = 0;
  vm.runInNewContext(script, {
    document: {
      querySelector: () => board,
      elementFromPoint: () => targetColumn,
    },
    requestAnimationFrame: (callback) => {
      frames.set(++frameId, callback);
      return frameId;
    },
    cancelAnimationFrame: (id) => frames.delete(id),
  });
  handlers.dragstart({
    target: card,
    dataTransfer: { setData() {}, effectAllowed: "" },
  });
  handlers.dragover({
    target: targetColumn,
    clientX: pointerX,
    clientY: 100,
    preventDefault() {},
    dataTransfer: { dropEffect: "" },
  });
  for (let i = 0; i < 4; i++) {
    const [id, callback] = frames.entries().next().value;
    frames.delete(id);
    callback();
  }
  const held = board.scrollLeft;
  handlers.dragend();
  return { held, pendingFrames: frames.size };
}

test("dragging at either Board edge scrolls until the drag ends", () => {
  const right = boardDrag(0, 390);
  const left = boardDrag(1000, 10);
  assert.ok(right.held > 0);
  assert.ok(left.held < 1000);
  assert.equal(right.pendingFrames, 0);
  assert.equal(left.pendingFrames, 0);
});
