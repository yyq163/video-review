export class ReviewDomainError extends Error {
  constructor(
    message: string,
    public readonly code: string,
  ) {
    super(message);
    this.name = 'ReviewDomainError';
  }
}

export function invariant(condition: unknown, message: string, code: string): asserts condition {
  if (!condition) {
    throw new ReviewDomainError(message, code);
  }
}
