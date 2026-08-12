-- PostgreSQL initialization script for Divine users
-- Creates customer and broker user tables with ID format checks

CREATE TABLE IF NOT EXISTS "DIVINE_CUSTOMER_USERS" (
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

CREATE TABLE IF NOT EXISTS "DIVINE_BROKER_USERS" (
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
