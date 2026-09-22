import {useEffect} from 'react';
import './Tooltip.css';

/**
 * The one floating tooltip. Any element with `data-tip` (and optionally `data-key`,
 * `data-side="right"`) shows it on hover and keyboard focus, and is described by it.
 * The element is created outside React so it can move into an open modal dialog,
 * the only layer above the page.
 */
export function Tooltip() {
  useEffect(() => {
    const tip = document.createElement('div');
    tip.id = 'tip';
    tip.setAttribute('role', 'tooltip');
    tip.hidden = true;
    document.body.append(tip);
    let anchor: HTMLElement | null = null;
    let leaving = 0;
    // A control whose tip changes under the pointer (Pause becomes Resume) re-shows it.
    const retip = new MutationObserver(() => {
      if (anchor) show(anchor);
    });

    const show = (element: HTMLElement) => {
      clearTimeout(leaving);
      anchor = element;
      retip.disconnect();
      retip.observe(element, {attributeFilter: ['data-tip']});
      const layer = element.closest('dialog') ?? document.body;
      if (tip.parentElement !== layer) layer.append(tip);
      tip.textContent = element.dataset.tip ?? '';
      if (element.dataset.key) {
        const key = document.createElement('kbd');
        key.textContent = element.dataset.key;
        tip.append(key);
      }
      tip.hidden = false;
      const box = element.getBoundingClientRect();
      const size = tip.getBoundingClientRect();
      let x = box.left + box.width / 2 - size.width / 2;
      let y = box.bottom + 8;
      if (element.dataset.side === 'right') {
        x = box.right + 10;
        y = box.top + box.height / 2 - size.height / 2;
      }
      x = Math.max(8, Math.min(x, innerWidth - size.width - 8));
      if (y + size.height > innerHeight - 8) y = box.top - size.height - 8;
      tip.style.left = `${x}px`;
      tip.style.top = `${y}px`;
      element.setAttribute('aria-describedby', 'tip');
    };
    const hide = () => {
      clearTimeout(leaving);
      retip.disconnect();
      anchor?.removeAttribute('aria-describedby');
      anchor = null;
      tip.hidden = true;
    };
    const find = (event: Event) =>
      event.target instanceof Element ? event.target.closest<HTMLElement>('[data-tip]') : null;
    // The tip is hoverable (WCAG 1.4.13): it hides only once the pointer has left both it and its
    // anchor, after a short delay that covers the gap between them.
    const over = (event: Event) => {
      const onTip = event.target instanceof Node && tip.contains(event.target);
      const element = onTip ? anchor : find(event);
      if (element === anchor) clearTimeout(leaving);
      else if (element) show(element);
      else {
        clearTimeout(leaving);
        leaving = window.setTimeout(hide, 100);
      }
    };
    const focus = (event: Event) => {
      const element = find(event);
      if (element) show(element);
      else hide();
    };
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') hide();
    };
    document.addEventListener('pointerover', over);
    document.addEventListener('focusin', focus);
    document.addEventListener('focusout', hide);
    document.addEventListener('keydown', key);
    document.addEventListener('scroll', hide, true);
    return () => {
      document.removeEventListener('pointerover', over);
      document.removeEventListener('focusin', focus);
      document.removeEventListener('focusout', hide);
      document.removeEventListener('keydown', key);
      document.removeEventListener('scroll', hide, true);
      clearTimeout(leaving);
      retip.disconnect();
      tip.remove();
    };
  }, []);
  return null;
}
