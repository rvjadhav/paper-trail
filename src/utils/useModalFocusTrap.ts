import { useEffect, type RefObject } from 'react';

const openModalStack: symbol[] = [];
let savedBodyOverflow: string | null = null;

function isElementVisible(el: HTMLElement): boolean {
  if (el.hasAttribute('hidden')) return false;
  if (el.closest('[hidden], [aria-hidden="true"]')) return false;
  try {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
  } catch {
    // Ignore computed style fallback issues in non-browser runtimes
  }
  return true;
}

export function getFocusableElements(container: HTMLElement): HTMLElement[] {
  const selector = [
    'a[href]',
    'button:not([disabled])',
    'input:not([disabled]):not([type="hidden"])',
    'select:not([disabled])',
    'textarea:not([disabled])',
    '[tabindex]:not([tabindex="-1"])',
  ].join(', ');

  return Array.from(container.querySelectorAll<HTMLElement>(selector)).filter(isElementVisible);
}

/**
 * Accessible focus trap & ref-counted body scroll lock for drawers and modals.
 * Stacked dialogs (e.g. Stock Dossier over Fund Profile) are handled via `openModalStack`
 * so only the topmost dialog traps Tab cycling while body scroll stays locked until all close.
 */
export function useModalFocusTrap(
  containerRef: RefObject<HTMLElement | null>,
  isOpen = true
): void {
  useEffect(() => {
    if (!isOpen) return;

    const token = Symbol('modal-focus-trap');
    const previousFocused =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;

    if (openModalStack.length === 0) {
      savedBodyOverflow = document.body.style.overflow;
      document.body.style.overflow = 'hidden';
    }
    openModalStack.push(token);

    const container = containerRef.current;
    if (container && !container.contains(document.activeElement)) {
      const focusables = getFocusableElements(container);
      const target = focusables[0] ?? container;
      try {
        target.focus({ preventScroll: true });
      } catch {
        target.focus();
      }
    }

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return;
      if (openModalStack[openModalStack.length - 1] !== token) return;
      const panel = containerRef.current;
      if (!panel) return;

      const focusables = getFocusableElements(panel);
      if (focusables.length === 0) {
        e.preventDefault();
        panel.focus();
        return;
      }

      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;

      if (e.shiftKey) {
        if (active === first || !panel.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else if (active === last || !panel.contains(active)) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', onKeyDown);

    return () => {
      document.removeEventListener('keydown', onKeyDown);
      const idx = openModalStack.lastIndexOf(token);
      if (idx !== -1) {
        openModalStack.splice(idx, 1);
      }
      if (openModalStack.length === 0) {
        document.body.style.overflow = savedBodyOverflow ?? '';
        savedBodyOverflow = null;
      }
      if (previousFocused && previousFocused.isConnected) {
        try {
          previousFocused.focus({ preventScroll: true });
        } catch {
          previousFocused.focus();
        }
      }
    };
  }, [containerRef, isOpen]);
}
