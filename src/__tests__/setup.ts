import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

afterEach(() => {
  cleanup();
  document.body.style.overflow = '';
  window.localStorage.clear();
  window.history.replaceState(null, '', '/');
});

if (typeof window.scrollTo !== 'function') {
  window.scrollTo = vi.fn();
} else {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
}

if (typeof window.matchMedia !== 'function') {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}
