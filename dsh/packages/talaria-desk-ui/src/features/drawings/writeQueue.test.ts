import { describe, expect, it } from "vitest";

import { createSerialQueue } from "./writeQueue";

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

describe("画线串行写队列", () => {
  it("后提交的写操作必须等前一个完成，顺序与提交一致", async () => {
    const queue = createSerialQueue();
    const order: string[] = [];
    const first = queue.run(async () => {
      order.push("start-1");
      await delay(10);
      order.push("end-1");
      return 1;
    });
    const second = queue.run(async () => {
      order.push("start-2");
      return 2;
    });
    await expect(Promise.all([first, second])).resolves.toEqual([1, 2]);
    expect(order).toEqual(["start-1", "end-1", "start-2"]);
  });

  it("单个写失败会回滚该次操作，但不阻塞后续写", async () => {
    const queue = createSerialQueue();
    const failed = queue.run(async () => {
      throw new Error("PATCH 失败");
    });
    const next = queue.run(async () => "ok");
    await expect(failed).rejects.toThrow("PATCH 失败");
    await expect(next).resolves.toBe("ok");
  });

  it("idle 在队列清空后 resolve", async () => {
    const queue = createSerialQueue();
    let done = false;
    void queue.run(async () => {
      await delay(5);
      done = true;
    });
    expect(done).toBe(false);
    await queue.idle();
    expect(done).toBe(true);
  });
});
