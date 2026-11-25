export function getRoute() {
  const parts = location.hash.slice(1).split('/');
  const page = parts[0] || '';
  const section = /^\d$/.test(page) && Number(page) < 6 ? Number(page) : 0;
  let runId = null;
  let shiftKey = null;
  if (parts[1] === 'shift' && parts[2]) {
    shiftKey = decodeURIComponent(parts[2]);
  } else if (/^[1-9]\d*$/.test(parts[1] || '')) {
    runId = Number(parts[1]);
  }
  return {section, runId, shiftKey};
}
