import type { FC, MouseEvent } from 'react';

export type ThemeMode = 'dark' | 'light';

interface ThemeToggleProps {
  theme: ThemeMode;
  switchAnim: { seq: number; to: ThemeMode } | null;
  onToggle: (e: MouseEvent<HTMLButtonElement>) => void;
}

export const ThemeToggle: FC<ThemeToggleProps> = ({ theme, switchAnim, onToggle }) => {
  const animClass = switchAnim ? `is-switching-${switchAnim.to}` : '';
  const nextLabel = theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';

  return (
    <button
      id="theme-toggle-btn"
      type="button"
      className={`theme-toggle ${theme} ${animClass}`.trim()}
      onClick={onToggle}
      aria-label={nextLabel}
      aria-pressed={theme === 'dark'}
      title={`${nextLabel} (Currently ${theme === 'dark' ? 'Dark' : 'Light'} mode)`}
    >
      <span
        key={switchAnim?.seq ?? 0}
        className="theme-toggle-burst"
        aria-hidden="true"
      />
      <span className="theme-toggle-track" aria-hidden="true">
        <span className="theme-toggle-thumb" />

        <span className={`theme-toggle-option ${theme === 'dark' ? 'active' : ''}`}>
          <span className="theme-icon-wrap moon-wrap">
            <svg
              className="theme-svg moon-svg"
              width="12"
              height="12"
              viewBox="0 0 16 16"
              fill="none"
            >
              <path
                className="moon-body"
                d="M6.07 3.82 A5 5 0 1 0 11.48 9.23 A3.9 3.9 0 0 1 6.07 3.82 Z"
                fill="currentColor"
              />
              <circle className="moon-star star-1" cx="11.5" cy="3.2" r="1.05" fill="currentColor" />
              <circle className="moon-star star-2" cx="13.8" cy="5.8" r="0.72" fill="currentColor" />
            </svg>
          </span>
          <span className="theme-opt-label">Dark</span>
        </span>

        <span className={`theme-toggle-option ${theme === 'light' ? 'active' : ''}`}>
          <span className="theme-icon-wrap sun-wrap">
            <svg
              className="theme-svg sun-svg"
              width="12"
              height="12"
              viewBox="0 0 16 16"
              fill="none"
            >
              <circle className="sun-core" cx="8" cy="8" r="3.1" fill="currentColor" />
              <g className="sun-rays" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <line x1="8" y1="1.5" x2="8" y2="3.1" />
                <line x1="8" y1="12.9" x2="8" y2="14.5" />
                <line x1="1.5" y1="8" x2="3.1" y2="8" />
                <line x1="12.9" y1="8" x2="14.5" y2="8" />
                <line x1="3.4" y1="3.4" x2="4.55" y2="4.55" />
                <line x1="11.45" y1="11.45" x2="12.6" y2="12.6" />
                <line x1="12.6" y1="3.4" x2="11.45" y2="4.55" />
                <line x1="3.4" y1="12.6" x2="4.55" y2="11.45" />
              </g>
            </svg>
          </span>
          <span className="theme-opt-label">Light</span>
        </span>
      </span>
    </button>
  );
};
