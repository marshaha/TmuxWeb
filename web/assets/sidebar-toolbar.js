// Keep the collapse button outside the scroller. Plugins append links inside the row.
for (const frame of document.querySelectorAll('.sidebar-tool-frame')) {
  const row = frame.firstElementChild;
  function updateEdges() {
    frame.classList.toggle('more-left', row.scrollLeft > 1);
    frame.classList.toggle('more-right', row.scrollWidth - row.clientWidth - row.scrollLeft > 1);
  }
  row.addEventListener('scroll', updateEdges, {passive: true});
  row.addEventListener('wheel', event => {
    if (event.ctrlKey || Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return;
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? row.clientWidth : 1;
    const before = row.scrollLeft;
    row.scrollLeft += event.deltaY * unit;
    if (row.scrollLeft !== before) event.preventDefault();
  }, {passive: false});
  new ResizeObserver(updateEdges).observe(row);
  new MutationObserver(updateEdges).observe(row, {childList: true});
  updateEdges();
}
