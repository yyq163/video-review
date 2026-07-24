export async function settleWithConcurrencyLimit<Input, Output>(
  inputs: readonly Input[],
  concurrencyLimit: number,
  operation: (input: Input, index: number) => Output | Promise<Output>,
): Promise<Array<PromiseSettledResult<Awaited<Output>>>> {
  if (inputs.length === 0) return [];
  const workerCount = Math.min(
    inputs.length,
    Math.max(1, Math.floor(concurrencyLimit)),
  );
  const results = new Array<PromiseSettledResult<Awaited<Output>>>(inputs.length);
  let nextIndex = 0;

  const runWorker = async () => {
    while (true) {
      const index = nextIndex;
      nextIndex += 1;
      if (index >= inputs.length) return;
      try {
        results[index] = {
          status: 'fulfilled',
          value: await operation(inputs[index], index),
        };
      } catch (reason) {
        results[index] = { status: 'rejected', reason };
      }
    }
  };

  await Promise.all(Array.from({ length: workerCount }, () => runWorker()));
  return results;
}
