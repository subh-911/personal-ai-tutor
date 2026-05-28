export type Role = "user" | "assistant";

export type Message = {
  id: string;
  role: Role;
  content: string;
};

export type ForceRoute = "tutor" | "quiz";

export type ChatPayload = {
  message: string;
  session_id?: string;
  force_route?: ForceRoute;
};

export const CHAT_ENDPOINT = "/api/backend/chat";
export const SESSION_STORAGE_KEY = "tutorSessionId";
