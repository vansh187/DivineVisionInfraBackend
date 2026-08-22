-- PostgreSQL initialization script for Divine users
-- Creates customer and broker user tables with ID format checks

CREATE TABLE IF NOT EXISTS divine_customer_users (
  id varchar(6) PRIMARY KEY CHECK (id ~ '^C[0-9]{5}$'),
  username varchar(255) UNIQUE NOT NULL,
  first_name varchar(150),
  last_name varchar(150),
  email varchar(255),
  phone varchar(50),
  password_hash varchar(255) NOT NULL,
  created_by varchar(255),
  created_date timestamptz DEFAULT now(),
  last_updated_by varchar(255),
  last_updated_date timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS divine_broker_users (
  id varchar(6) PRIMARY KEY CHECK (id ~ '^B[0-9]{5}$'),
  username varchar(255) UNIQUE NOT NULL,
  first_name varchar(150),
  last_name varchar(150),
  email varchar(255),
  phone varchar(50),
  password_hash varchar(255) NOT NULL,
  created_by varchar(255),
  created_date timestamptz DEFAULT now(),
  last_updated_by varchar(255),
  last_updated_date timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS divine_documents (
  id varchar(36) PRIMARY KEY,
  owner_id varchar(6) NOT NULL,
  owner_role varchar(10) NOT NULL CHECK (owner_role IN ('customer', 'broker')),
  document_type varchar(100) NOT NULL,
  form_data jsonb NOT NULL,
  storage_path text NOT NULL,
  status varchar(20) NOT NULL DEFAULT 'generated',
  storage_bucket varchar(100),
  project_id varchar(100),
  payment_id varchar(36),
  razorpay_order_id varchar(64),
  razorpay_payment_id varchar(64),
  created_date timestamptz DEFAULT now(),
  last_updated_date timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_divine_documents_owner_id ON divine_documents (owner_id);
CREATE INDEX IF NOT EXISTS idx_divine_documents_payment_id ON divine_documents (payment_id);

CREATE TABLE IF NOT EXISTS divine_kyc_verifications (
  id varchar(36) PRIMARY KEY,
  owner_id varchar(6) NOT NULL,
  owner_role varchar(10) NOT NULL CHECK (owner_role IN ('customer', 'broker')),
  method varchar(20) NOT NULL CHECK (method IN ('qr', 'offline_xml')),
  verified boolean NOT NULL,
  masked_aadhaar varchar(20) NOT NULL,
  extracted_data jsonb NOT NULL,
  failure_reason varchar(100),
  created_date timestamptz DEFAULT now()
);
-- Note: by design, no column here ever holds a full/plaintext Aadhaar number - both the
-- Secure QR and Offline e-KYC XML formats only ever expose the last 4 digits (see
-- masked_aadhaar), never the full number, so there is nothing further to redact.

CREATE INDEX IF NOT EXISTS idx_divine_kyc_verifications_owner_id ON divine_kyc_verifications (owner_id);

CREATE TABLE IF NOT EXISTS divine_payments (
  id varchar(36) PRIMARY KEY,
  owner_id varchar(6) NOT NULL,
  owner_role varchar(10) NOT NULL CHECK (owner_role IN ('customer', 'broker')),
  amount numeric(12,2) NOT NULL,
  currency varchar(3) NOT NULL DEFAULT 'INR',
  status varchar(20) NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'paid', 'failed')),
  method varchar(20) NOT NULL DEFAULT 'razorpay' CHECK (method IN ('razorpay', 'cash')),
  razorpay_order_id varchar(64),
  razorpay_payment_id varchar(64),
  razorpay_signature varchar(255),
  notes jsonb,
  created_date timestamptz DEFAULT now(),
  last_updated_date timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_divine_payments_owner_id ON divine_payments (owner_id);
CREATE INDEX IF NOT EXISTS idx_divine_payments_razorpay_order_id ON divine_payments (razorpay_order_id);

CREATE TABLE IF NOT EXISTS divine_site_visits (
  id varchar(36) PRIMARY KEY,
  broker_id varchar(6) NOT NULL,
  customer_name varchar(200) NOT NULL,
  customer_contact varchar(200),
  visit_date date NOT NULL,
  visit_time varchar(5) NOT NULL,
  notes text,
  status varchar(20) NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'cancelled')),
  created_date timestamptz DEFAULT now(),
  last_updated_date timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_divine_site_visits_broker_id ON divine_site_visits (broker_id);

CREATE TABLE IF NOT EXISTS divine_market_trends (
  id varchar(36) PRIMARY KEY,
  city varchar(100) NOT NULL,
  locality varchar(150),
  property_type varchar(80) NOT NULL,
  period_label varchar(50) NOT NULL,
  as_of_date date NOT NULL,
  price_per_sqyd numeric(12,2) NOT NULL CHECK (price_per_sqyd > 0),
  previous_price_per_sqyd numeric(12,2) CHECK (previous_price_per_sqyd IS NULL OR previous_price_per_sqyd > 0),
  rental_yield_percent numeric(5,2) CHECK (rental_yield_percent IS NULL OR rental_yield_percent >= 0),
  demand_score numeric(5,2) CHECK (demand_score IS NULL OR demand_score BETWEEN 0 AND 100),
  supply_score numeric(5,2) CHECK (supply_score IS NULL OR supply_score BETWEEN 0 AND 100),
  sample_size integer NOT NULL DEFAULT 0 CHECK (sample_size >= 0),
  created_date timestamptz DEFAULT now(),
  last_updated_date timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_divine_market_trends_filters
  ON divine_market_trends (city, locality, property_type, as_of_date DESC);

CREATE TABLE IF NOT EXISTS divine_broker_commissions (
  id varchar(36) PRIMARY KEY CHECK (id ~ '^com_[0-9a-f]{32}$'),
  broker_id varchar(80) NOT NULL,
  serial_number varchar(100) NOT NULL,
  unit_address varchar(500) NOT NULL,
  customer_name varchar(200),
  township varchar(200),
  sale_value numeric(14,2) CHECK (sale_value IS NULL OR sale_value >= 0),
  commission_amount numeric(14,2) NOT NULL CHECK (commission_amount > 0),
  status varchar(20) NOT NULL CHECK (status IN ('pending', 'paid', 'rejected')),
  transaction_mode varchar(20) NOT NULL CHECK (transaction_mode IN ('cash', 'booking')),
  razorpay_order_id varchar(64),
  created_at timestamptz NOT NULL DEFAULT now(),
  paid_at timestamptz,
  rejected_at timestamptz,
  last_updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_divine_broker_commissions_broker_id
  ON divine_broker_commissions (broker_id);
CREATE INDEX IF NOT EXISTS idx_divine_broker_commissions_status
  ON divine_broker_commissions (status);
CREATE INDEX IF NOT EXISTS idx_divine_broker_commissions_created_at
  ON divine_broker_commissions (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_divine_broker_commissions_razorpay_order_id
  ON divine_broker_commissions (razorpay_order_id);

-- ============================================================
-- AI Concierge Chatbot (Phase 1: web widget only)
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS divine_chatbot_leads (
  id varchar(36) PRIMARY KEY,
  channel varchar(10) NOT NULL DEFAULT 'web' CHECK (channel IN ('web')),
  visitor_name varchar(200),
  visitor_phone varchar(20),
  visitor_email varchar(255),
  linked_customer_id varchar(6) REFERENCES divine_customer_users(id),
  lead_temperature varchar(10) NOT NULL DEFAULT 'cold' CHECK (lead_temperature IN ('hot','warm','cold')),
  assigned_broker_id varchar(6) REFERENCES divine_broker_users(id),
  consent_given boolean NOT NULL DEFAULT false,
  consent_at timestamptz,
  notify_updates_opt_in boolean,
  created_date timestamptz DEFAULT now(),
  last_updated_date timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chatbot_leads_phone ON divine_chatbot_leads (visitor_phone);
CREATE INDEX IF NOT EXISTS idx_chatbot_leads_email ON divine_chatbot_leads (visitor_email);
CREATE INDEX IF NOT EXISTS idx_chatbot_leads_temperature ON divine_chatbot_leads (lead_temperature);

CREATE TABLE IF NOT EXISTS divine_chatbot_lead_sources (
  id varchar(36) PRIMARY KEY,
  lead_id varchar(36) NOT NULL REFERENCES divine_chatbot_leads(id),
  ip_address inet,
  ip_geo_city varchar(120),
  ip_geo_region varchar(120),
  precise_lat double precision,
  precise_long double precision,
  maps_link text,
  referrer text,
  utm_source varchar(120),
  utm_medium varchar(120),
  utm_campaign varchar(120),
  device_type varchar(40),
  captured_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chatbot_lead_sources_lead_id ON divine_chatbot_lead_sources (lead_id);

CREATE TABLE IF NOT EXISTS divine_chatbot_sessions (
  id varchar(36) PRIMARY KEY,
  lead_id varchar(36) REFERENCES divine_chatbot_leads(id),
  callback_state varchar(20) CHECK (callback_state IN ('awaiting_name','awaiting_phone','awaiting_time','complete','awaiting_email','email_complete')),
  callback_name varchar(200),
  callback_phone varchar(20),
  callback_time varchar(100),
  auth_state varchar(80),
  auth_payload text,
  menu_state varchar(80),
  menu_payload text,
  loan_payload text,
  created_date timestamptz DEFAULT now(),
  last_activity_date timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chatbot_sessions_lead_id ON divine_chatbot_sessions (lead_id);
CREATE INDEX IF NOT EXISTS idx_chatbot_sessions_last_activity ON divine_chatbot_sessions (last_activity_date DESC);

CREATE TABLE IF NOT EXISTS divine_loan_reports (
  id varchar(36) PRIMARY KEY,
  session_id varchar(36) REFERENCES divine_chatbot_sessions(id),
  lead_id varchar(36) REFERENCES divine_chatbot_leads(id),
  snapshot_json text NOT NULL,
  created_date timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_loan_reports_session_id ON divine_loan_reports (session_id);

CREATE TABLE IF NOT EXISTS divine_chatbot_messages (
  id varchar(36) PRIMARY KEY,
  session_id varchar(36) NOT NULL REFERENCES divine_chatbot_sessions(id),
  role varchar(10) NOT NULL CHECK (role IN ('user','assistant','tool')),
  content text NOT NULL,
  tool_name varchar(80),
  llm_provider varchar(10) CHECK (llm_provider IN ('gemini','groq')),
  guardrail_score numeric(3,2),
  guardrail_passed boolean,
  latency_ms integer,
  created_date timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chatbot_messages_session_id ON divine_chatbot_messages (session_id, created_date);

CREATE TABLE IF NOT EXISTS divine_chatbot_qualification (
  id varchar(36) PRIMARY KEY,
  lead_id varchar(36) NOT NULL REFERENCES divine_chatbot_leads(id),
  budget_min numeric(14,2),
  budget_max numeric(14,2),
  unit_type varchar(80),
  timeline_days integer,
  intent_signal varchar(120),
  temperature varchar(10) CHECK (temperature IN ('hot','warm','cold')),
  buyer_type varchar(20) CHECK (buyer_type IS NULL OR buyer_type IN ('end_client','investor_dealer')),
  working_profile_type varchar(80),
  location_preference varchar(200),
  opportunity_type varchar(80),
  investment_size_band varchar(40),
  investment_goal varchar(80),
  proceed_preference varchar(40),
  source_flow varchar(20) CHECK (source_flow IS NULL OR source_flow IN ('llm_signals','menu_sales','menu_browsing')),
  updated_date timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chatbot_qualification_lead_id ON divine_chatbot_qualification (lead_id);

CREATE TABLE IF NOT EXISTS divine_chatbot_callback_requests (
  id varchar(36) PRIMARY KEY,
  lead_id varchar(36) NOT NULL REFERENCES divine_chatbot_leads(id),
  visitor_name varchar(200) NOT NULL,
  phone varchar(20) NOT NULL,
  preferred_time varchar(100) NOT NULL,
  status varchar(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','contacted','done')),
  notes text,
  request_type varchar(20) NOT NULL DEFAULT 'callback' CHECK (request_type IN ('callback','support')),
  requested_at timestamptz DEFAULT now(),
  actioned_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_chatbot_callback_requests_status ON divine_chatbot_callback_requests (status);
CREATE INDEX IF NOT EXISTS idx_chatbot_callback_requests_lead_id ON divine_chatbot_callback_requests (lead_id);

CREATE TABLE IF NOT EXISTS divine_chatbot_kb_documents (
  id varchar(36) PRIMARY KEY,
  title varchar(300) NOT NULL,
  category varchar(80) NOT NULL,
  source_uri text,
  created_date timestamptz DEFAULT now(),
  last_updated_date timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS divine_chatbot_kb_chunks (
  id varchar(36) PRIMARY KEY,
  document_id varchar(36) NOT NULL REFERENCES divine_chatbot_kb_documents(id),
  chunk_index integer NOT NULL,
  content text NOT NULL,
  embedding vector(768) NOT NULL,
  created_date timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chatbot_kb_chunks_document_id ON divine_chatbot_kb_chunks (document_id);
-- HNSW (not ivfflat): ivfflat's recall degrades badly on a small/young table (its list
-- partitioning assumes real row volume) - a freshly-seeded KB would silently return zero
-- matches on every search. HNSW has no such "too few rows" failure mode.
CREATE INDEX IF NOT EXISTS idx_chatbot_kb_chunks_embedding
  ON divine_chatbot_kb_chunks USING hnsw (embedding vector_cosine_ops);
