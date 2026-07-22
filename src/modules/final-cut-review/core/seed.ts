import { buildSeedData } from './seed-fixture';
import type { SeedData } from './seed-types';

export type { SeedData } from './seed-types';

export function createSeedData(): SeedData {
  return buildSeedData();
}
