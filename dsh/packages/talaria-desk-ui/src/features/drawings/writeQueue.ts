/**
 * 单一异步串行写队列：前端所有画线写操作（创建/移动/样式/删除/撤销）
 * 都从这里排队，避免 PATCH 与 DELETE 乱序覆盖版本链。
 * 单次失败只让该次调用 reject，链本身继续，后续写仍会执行。
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
