import '@testing-library/jest-dom/vitest';
import { createUuid } from '../modules/final-cut-review/core/uuid';

if (!URL.createObjectURL) {
  URL.createObjectURL = () => `blob:mock-${createUuid()}`;
}

if (!URL.revokeObjectURL) {
  URL.revokeObjectURL = () => undefined;
}

Object.defineProperty(HTMLMediaElement.prototype, 'play', {
  configurable: true,
  value: () => Promise.resolve(),
});

Object.defineProperty(HTMLMediaElement.prototype, 'pause', {
  configurable: true,
  value: () => undefined,
});
