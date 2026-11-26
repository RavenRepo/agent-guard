import { readFileSync, writeFileSync } from 'node:fs';
import { classify, REPORT_VERSION } from '../lib/classify.mjs';

const messages = JSON.parse(readFileSync(0, 'utf8'));
const classes = messages.map(classify);
writeFileSync('report.json', JSON.stringify({ version: REPORT_VERSION, classes }));
process.exit(classes.includes('assertion') ? 1 : 0);
