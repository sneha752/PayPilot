/**
 * Mirrors backend `app/services/product_service.py::Product`.
 *
 * The first block of fields is common to every category; the rest are
 * category-specific and therefore optional/nullable.
 */
export interface Product {
  // Common fields (present for every product).
  id: string;
  name: string;
  category: string;
  brand: string;
  price_inr: number;
  rating: number;
  description: string;

  // Laptop / smartphone / tablet specs.
  ram_gb?: number | null;
  storage_gb?: number | null;
  processor?: string | null;
  gpu?: string | null;
  screen_size_in?: number | null;
  battery_mah?: number | null;
  camera_mp?: number | null;

  // Headphone specs.
  wireless?: boolean | null;
  noise_cancelling?: boolean | null;
  battery_hours?: number | null;
  form_factor?: string | null;

  // Monitor specs.
  resolution?: string | null;
  refresh_rate_hz?: number | null;
  panel_type?: string | null;
}
