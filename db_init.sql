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
  created_date timestamptz DEFAULT now(),
  last_updated_date timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_divine_documents_owner_id ON divine_documents (owner_id);

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
