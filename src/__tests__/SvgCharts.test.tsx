import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { EquityCurveChart, SectorBreakdownBar } from '../components/SvgCharts';

describe('SvgCharts', () => {
  it('supports keyboard Arrow/Home/End/Escape crosshair scrubbing and pointer events', () => {
    const series = [
      {
        id: 'fund',
        label: 'Test Fund',
        color: '#2997ff',
        points: [
          { date: '2025-01-15', nav: 100 },
          { date: '2025-06-15', nav: 115 },
          { date: '2026-01-15', nav: 130 },
        ],
      },
      {
        id: 'late-bench',
        label: 'Late Start',
        color: '#00c7be',
        dashed: true,
        points: [
          { date: '2025-06-15', nav: 200 },
          { date: '2026-01-15', nav: 220 },
        ],
      },
    ];

    const { container } = render(<EquityCurveChart series={series} defaultWindow="2Y" />);

    const svg = container.querySelector('svg.chart-svg') as SVGSVGElement;
    expect(svg).toBeInTheDocument();
    expect(svg.getAttribute('aria-label')).toContain('2026-01-15');
    expect(screen.queryByTestId('chart-crosshair')).not.toBeInTheDocument();

    // Verify paths are emitted and do not originate at (0,0) across missing leading dates
    const latePath = container.querySelector('path[data-series-id="late-bench"]');
    expect(latePath).not.toBeNull();
    const dAttr = latePath?.getAttribute('d') ?? '';
    expect(dAttr.startsWith('M')).toBe(true);
    expect(dAttr.includes('L')).toBe(true);

    // Keyboard scrubbing: ArrowLeft moves from index 2 -> index 1 ('2025-06-15')
    fireEvent.keyDown(svg, { key: 'ArrowLeft' });
    expect(screen.getByTestId('chart-crosshair')).toBeInTheDocument();
    expect(svg.getAttribute('aria-label')).toContain('2025-06-15');

    // Home moves to earliest point where Late Start has null (only 1 circle should render)
    fireEvent.keyDown(svg, { key: 'Home' });
    expect(svg.getAttribute('aria-label')).toContain('2025-01-15');
    expect(screen.getByTestId('chart-crosshair').querySelectorAll('circle')).toHaveLength(1);

    // End moves to latest point where both series exist (2 circles render)
    fireEvent.keyDown(svg, { key: 'End' });
    expect(svg.getAttribute('aria-label')).toContain('2026-01-15');
    expect(screen.getByTestId('chart-crosshair').querySelectorAll('circle')).toHaveLength(2);

    // Escape clears crosshair
    fireEvent.keyDown(svg, { key: 'Escape' });
    expect(screen.queryByTestId('chart-crosshair')).not.toBeInTheDocument();

    // Pointer scrubbing across mocked SVG bounding box
    vi.spyOn(svg, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      top: 0,
      left: 0,
      right: 760,
      bottom: 260,
      width: 760,
      height: 260,
      toJSON: () => ({}),
    });
    fireEvent.pointerDown(svg, { clientX: 54, pointerId: 1, pointerType: 'mouse' });
    expect(screen.getByTestId('chart-crosshair')).toBeInTheDocument();
    expect(svg.getAttribute('aria-label')).toContain('2025-01-15');
  });

  it('groups sectors beyond top 6 into an explicit "Other (N)" legend bucket', () => {
    const sectors = [
      { sector: 'Semiconductors', weightPct: 29, stockCount: 4 },
      { sector: 'Enterprise Software', weightPct: 24, stockCount: 3 },
      { sector: 'Platforms & Interactive', weightPct: 16, stockCount: 2 },
      { sector: 'Hardware & IT Services', weightPct: 10, stockCount: 2 },
      { sector: 'Fintech & Payments', weightPct: 8, stockCount: 1 },
      { sector: 'Healthcare', weightPct: 6, stockCount: 1 },
      { sector: 'Energy', weightPct: 4, stockCount: 1 },
      { sector: 'Utilities', weightPct: 3, stockCount: 1 },
    ];

    render(<SectorBreakdownBar sectors={sectors} />);
    expect(screen.getByText('Semiconductors')).toBeInTheDocument();
    expect(screen.getByText('Healthcare')).toBeInTheDocument();
    expect(screen.getByText('Other (2)')).toBeInTheDocument();
    expect(screen.getByText('7.0%')).toBeInTheDocument();
    expect(screen.queryByText('Energy')).not.toBeInTheDocument();
  });
});
