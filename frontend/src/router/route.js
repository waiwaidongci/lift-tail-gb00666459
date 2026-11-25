export function getRoute() {
  const [page,key] = location.hash.slice(1).split('/');
  let section = /^\d$/.test(page) ? Number(page) : 0;
  if (page === 'reconcile' || page === 'reconciliation' || page === 'duizhang') section = 6;
  if (section > 6) section = 0;
  const runId = /^[1-9]\d*$/.test(key || '') ? Number(key) : null;
  return {section,runId};
}
