import type { Capability, EntryMode } from '../contracts/types';
import { EDIT_ENTRY_PROFILE, REVIEW_ENTRY_PROFILE } from '../contracts-generated/backend-contract';

const editCapabilities = [...EDIT_ENTRY_PROFILE] as Capability[];
const reviewCapabilities = [...REVIEW_ENTRY_PROFILE] as Capability[];

export function getCapabilities(mode: EntryMode): ReadonlySet<Capability> {
  return new Set(mode === 'edit' ? editCapabilities : reviewCapabilities);
}

export function hasCapability(mode: EntryMode, capability: Capability): boolean {
  return getCapabilities(mode).has(capability);
}
