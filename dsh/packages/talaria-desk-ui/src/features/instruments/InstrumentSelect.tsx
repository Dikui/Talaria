import { useEffect, useRef, useState } from "react";

import type { InstrumentSpec } from "@talaria/contracts";

interface InstrumentSelectProps {
  instruments: InstrumentSpec[];
  value: string;
  disabled?: boolean;
  onChange: (instrumentId: string) => void;
}

/**
 * 标的切换器。长标识（如 1000BONK-USDT-PERP）用 ellipsis + title，
 * 下拉菜单不允许撑破 390px 视口。
 */
export function InstrumentSelect({
  instruments,
  value,
  disabled = false,
  onChange,
}: InstrumentSelectProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    const handlePointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [open]);

  return (
    <div className="instrument-field" ref={rootRef} title={value}>
      <span className="field-label">标的</span>
      <button
        type="button"
        className="instrument-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label="选择标的"
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="instrument-current" data-testid="current-instrument">
          {value}
        </span>
        <span className="chevron" aria-hidden="true">
          ▾
        </span>
      </button>
      {open && (
        <ul className="instrument-menu" role="listbox" aria-label="候选标的">
          {instruments.length === 0 ? (
            <li className="instrument-option is-empty">暂无可选标的</li>
          ) : (
            instruments.map((instrument) => (
              <li key={instrument.instrument_id}>
                <button
                  type="button"
                  role="option"
                  aria-selected={instrument.instrument_id === value}
                  className={`instrument-option${
                    instrument.instrument_id === value ? " is-selected" : ""
                  }`}
                  title={instrument.instrument_id}
                  onClick={() => {
                    onChange(instrument.instrument_id);
                    setOpen(false);
                  }}
                >
                  {instrument.instrument_id}
                </button>
              </li>
            ))
          )}
        </ul>
      )}
    </div>
  );
}
