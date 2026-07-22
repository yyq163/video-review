import { afterEach, describe, expect, it, vi } from 'vitest';
import { createUuid } from './uuid';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('createUuid', () => {
  it('prefers the native randomUUID implementation when it is available', () => {
    const expected = '123e4567-e89b-42d3-a456-426614174000';
    const randomUUID = vi.fn(() => expected);
    const getRandomValues = vi.fn();
    vi.stubGlobal('crypto', { randomUUID, getRandomValues });

    expect(createUuid()).toBe(expected);
    expect(randomUUID).toHaveBeenCalledOnce();
    expect(getRandomValues).not.toHaveBeenCalled();
  });

  it('builds an RFC 4122 version-4 UUID from getRandomValues when randomUUID is unavailable', () => {
    const getRandomValues = vi.fn((target: Uint8Array) => {
      target.set(Array.from({ length: 16 }, (_, index) => index));
      return target;
    });
    const mathRandom = vi.spyOn(Math, 'random');
    vi.stubGlobal('crypto', { getRandomValues });

    const value = createUuid();

    expect(getRandomValues).toHaveBeenCalledOnce();
    expect(getRandomValues.mock.calls[0]?.[0]).toBeInstanceOf(Uint8Array);
    expect(getRandomValues.mock.calls[0]?.[0]).toHaveLength(16);
    expect(value).toBe('00010203-0405-4607-8809-0a0b0c0d0e0f');
    expect(value).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(mathRandom).not.toHaveBeenCalled();
  });

  it('fails closed when no cryptographic random source exists', () => {
    const mathRandom = vi.spyOn(Math, 'random');
    vi.stubGlobal('crypto', undefined);

    expect(() => createUuid()).toThrow('安全随机数生成器不可用，无法生成 UUID');
    expect(mathRandom).not.toHaveBeenCalled();
  });
});
