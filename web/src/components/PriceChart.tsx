import { ColorType, LineSeries, LineStyle, createChart, createSeriesMarkers, type UTCTimestamp } from 'lightweight-charts';
import { useEffect, useMemo, useRef } from 'react';

interface Props { history: { t: number; p: number }[]; entryTs: number; entryPrice: number; maxEntry: number | null }

function dedupe(history: { t: number; p: number }[]) {
  const out: { time: UTCTimestamp; value: number }[] = [];
  let last = -Infinity;
  for (const h of [...history].sort((a, b) => a.t - b.t)) {
    if (h.t > last) {
      out.push({ time: h.t as UTCTimestamp, value: h.p });
      last = h.t;
    }
  }
  return out;
}

export function PriceChart({ history, entryTs, entryPrice, maxEntry }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const points = useMemo(() => dedupe(history), [history]);

  useEffect(() => {
    if (!ref.current || points.length < 2) return;
    const chart = createChart(ref.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: '#6B7785', fontFamily: "'IBM Plex Mono', monospace", fontSize: 11 },
      grid: { vertLines: { color: '#1C242C' }, horzLines: { color: '#1C242C' } },
      rightPriceScale: { borderColor: '#1C242C' },
      timeScale: { borderColor: '#1C242C', timeVisible: true },
      crosshair: { vertLine: { color: '#4FC3F7' }, horzLine: { color: '#4FC3F7' } },
    });
    const series = chart.addSeries(LineSeries, { color: '#4FC3F7', lineWidth: 2, priceFormat: { type: 'price', precision: 3, minMove: 0.001 } });
    series.setData(points);
    const nearest = points.reduce((best, p) => (Math.abs(p.time - entryTs) < Math.abs(best.time - entryTs) ? p : best), points[0]);
    createSeriesMarkers(series, [{ time: nearest.time, position: 'belowBar', color: '#FFB000', shape: 'arrowUp', text: `WHALE ${entryPrice.toFixed(3)}` }]);
    if (maxEntry != null) {
      series.createPriceLine({ price: maxEntry, color: '#3DDC84', lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: 'MAX ENTRY' });
    }
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [points, entryTs, entryPrice, maxEntry]);

  if (points.length < 2) return <p className="empty">NO PRICE HISTORY</p>;
  return <div className="chart" ref={ref} />;
}
