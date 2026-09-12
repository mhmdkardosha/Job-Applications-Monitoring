(() => {
  const board = document.querySelector(".board");
  if (!board) return;

  let dragged = null;
  let pointerX = 0;
  let pointerY = 0;
  let scrollFrame = 0;
  const highlight = (column) => {
    board.querySelector(".is-drop-target")?.classList.remove("is-drop-target");
    if (column && dragged.closest(".board-col") !== column) {
      column.classList.add("is-drop-target");
    }
  };
  const scrollNearEdge = () => {
    scrollFrame = 0;
    if (!dragged) return;
    const rect = board.getBoundingClientRect();
    if (
      pointerX < rect.left || pointerX > rect.right ||
      pointerY < rect.top || pointerY > rect.bottom
    ) return;
    const direction = pointerX < rect.left + 64 ? -1 : pointerX > rect.right - 64 ? 1 : 0;
    if (!direction) return;
    const before = board.scrollLeft;
    board.scrollLeft += direction * 18;
    if (board.scrollLeft !== before) {
      highlight(document.elementFromPoint(pointerX, pointerY)?.closest(".board-col"));
      scrollFrame = requestAnimationFrame(scrollNearEdge);
    }
  };
  const clearDrag = () => {
    cancelAnimationFrame(scrollFrame);
    scrollFrame = 0;
    dragged?.classList.remove("is-dragging");
    highlight(null);
    dragged = null;
  };

  board.addEventListener("dragstart", (event) => {
    const card = event.target.closest(".board-card");
    if (!card || event.target.closest("form")) return;
    dragged = card;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", "board-card");
    card.classList.add("is-dragging");
  });

  board.addEventListener("dragover", (event) => {
    if (!dragged) return;
    event.preventDefault();
    pointerX = event.clientX;
    pointerY = event.clientY;
    highlight(event.target.closest(".board-col"));
    event.dataTransfer.dropEffect = "move";
    if (!scrollFrame) scrollFrame = requestAnimationFrame(scrollNearEdge);
  });

  board.addEventListener("dragleave", (event) => {
    const rect = board.getBoundingClientRect();
    if (
      event.clientX < rect.left || event.clientX > rect.right ||
      event.clientY < rect.top || event.clientY > rect.bottom
    ) {
      cancelAnimationFrame(scrollFrame);
      scrollFrame = 0;
      highlight(null);
    }
  });

  board.addEventListener("drop", (event) => {
    if (!dragged) return;
    event.preventDefault();
    const column = event.target.closest(".board-col");
    const card = dragged;
    clearDrag();
    if (!column || card.closest(".board-col") === column) return;
    const form = card.querySelector("form");
    form.elements.stage.value = column.dataset.stage;
    form.requestSubmit();
  });

  board.addEventListener("dragend", clearDrag);
})();
