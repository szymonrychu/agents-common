CREATE TABLE items (
    id bigserial PRIMARY KEY,
    status text NOT NULL DEFAULT 'queued',
    note text
);
