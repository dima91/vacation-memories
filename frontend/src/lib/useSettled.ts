import { useEffect, useState } from "react";

/** `value` once it has stopped changing for `delay` ms: a slider that is dragged asks for one preview, not one per step. */
export function useSettled<T>(value: T, delay: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return settled;
}
