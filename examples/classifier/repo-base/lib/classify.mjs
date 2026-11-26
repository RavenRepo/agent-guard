export const REPORT_VERSION = 2;

export function classify(message) {
  if (/timed? ?out/i.test(message)) return 'timeout';
  if (/out of memory|oom/i.test(message)) return 'oom';
  if (/econnrefused|connection/i.test(message)) return 'connection';
  return 'assertion';
}
