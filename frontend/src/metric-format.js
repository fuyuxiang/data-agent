/** Keep decimal strings exact; converting SQL DECIMAL values to Number loses cents. */
export function metricFormatter(pattern, fallback = value => String(value)) {
  const match = /^(,)?\.(\d{1,2})([f%])$/.exec(pattern || '');
  const digits = match ? Math.min(Number(match[2]), 20) : null;
  const formatter = match && new Intl.NumberFormat('zh-CN', {
    style: match[3] === '%' ? 'percent' : 'decimal', useGrouping: !!match[1],
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
  return value => {
    if (value == null || value === '' || typeof value === 'number' && Number.isNaN(value)) return '—';
    if (typeof value === 'string' && !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(value)) return fallback(value);
    if (!['number', 'bigint', 'string'].includes(typeof value)) return fallback(value);
    if (!formatter || !Number.isFinite(Number(value))) return fallback(value);
    return formatter.format(value);
  };
}
