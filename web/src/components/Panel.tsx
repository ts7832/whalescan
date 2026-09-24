import type { ReactNode } from 'react';

interface Props { code: string; title: string; right?: ReactNode; className?: string; children: ReactNode }

export function Panel({ code, title, right, className = '', children }: Props) {
  return (
    <section className={`panel ${className}`}>
      <header className="panel-head">
        <span className="panel-code">{code}</span>
        <h2>{title}</h2>
        <div className="panel-right">{right}</div>
      </header>
      <div className="panel-body">{children}</div>
    </section>
  );
}
