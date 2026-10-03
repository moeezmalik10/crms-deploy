import { createClient } from "@supabase/supabase-js";

// Set VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY in Vercel (use the "anon" key, never service_role).
const supabaseUrl = import.meta.env.VITE_SUPABASE_URL;
const supabaseKey = import.meta.env.VITE_SUPABASE_ANON_KEY;

export const supabase = createClient(supabaseUrl, supabaseKey);
