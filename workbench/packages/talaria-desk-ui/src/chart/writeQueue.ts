/**
 * Single asynchronous serial write queue for drawing persistence mutations.
 * Prevents out-of-order PATCH/DELETE/POST races against drawing version chains.
 */
export interface SerialWriteQueue {
  run<T>(task: () => Promise<T>): Promise<T>;
  idle(): Promise<void>;
}

export function createSerialQueue(): SerialWriteQueue {
  let tail: Promise<unknown> = Promise.resolve();
  return {
    run<T>(task: () => Promise<T>): Promise<T> {
      const result = tail.then(task, task);
      tail = result.then(
        () => undefined,
        () => undefined,
      );
      return result;
    },
    async idle(): Promise<void> {
      await tail;
    },
  };
}
