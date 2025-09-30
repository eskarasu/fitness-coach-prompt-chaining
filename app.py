#!/usr/bin/env python3

import argparse
import json
import re
import traceback
import os
import sys
from typing import Dict, Any, Optional
import google.generativeai as genai
from dotenv import load_dotenv
from config.prompts import PLANNING_PROMPT, EXECUTION_PROMPT

load_dotenv()


class FitnessCoach:
    def __init__(self, temperature: float = 0.7, top_p: float = 0.9, max_tokens: int = 2048):
        self.api_key = os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY environment variable not found")

        genai.configure(api_key=self.api_key)

        self.generation_config = genai.GenerationConfig(
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=max_tokens,
        )

        self.model = genai.GenerativeModel(
            model_name='gemini-2.5-flash',
            generation_config=self.generation_config,
        )

    def fix_json(self, json_str: str) -> Optional[str]:
        try:
            s = (json_str or "").strip()
            s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
            s = re.sub(r"\s*```$", "", s)
            s = s.strip()

            try:
                parsed = json.loads(s)
                return json.dumps(parsed, ensure_ascii=False, indent=2)
            except Exception:
                pass

            start = s.find('{')
            if start != -1:
                depth = 0
                end_index = None
                for i in range(start, len(s)):
                    ch = s[i]
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            end_index = i
                            break

                if end_index is not None:
                    candidate = s[start:end_index + 1]
                    try:
                        parsed = json.loads(candidate)
                        return json.dumps(parsed, ensure_ascii=False, indent=2)
                    except Exception:
                        pass

                candidate = s[start:]
                candidate = re.sub(r"```$", "", candidate).rstrip()

                def close_open_quotes(text: str) -> str:
                    cnt = 0
                    i = 0
                    while i < len(text):
                        if text[i] == '"':
                            j = i - 1
                            esc = 0
                            while j >= 0 and text[j] == '\\':
                                esc += 1
                                j -= 1
                            if esc % 2 == 0:
                                cnt += 1
                        i += 1
                    if cnt % 2 == 1:
                        return text + '"'
                    return text

                candidate = close_open_quotes(candidate)

                open_braces = candidate.count('{')
                close_braces = candidate.count('}')
                if close_braces < open_braces:
                    candidate = candidate + ('}' * (open_braces - close_braces))

                candidate = re.sub(r",\s*(\]|\})", r"\1", candidate)

                try:
                    parsed = json.loads(candidate)
                    return json.dumps(parsed, ensure_ascii=False, indent=2)
                except Exception:
                    pass

            try:
                inner = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE).strip()
                inner = re.sub(r"\s*```$", "", inner).strip()
                if inner.startswith('{'):
                    inner = inner[1:]
                lines = [ln.strip().strip('\",') for ln in inner.splitlines() if ln.strip()]
                steps = [ln for ln in lines if len(ln) > 10]
                if steps:
                    fallback = {
                        "steps": steps,
                        "assumptions": [],
                        "success_criteria": [],
                    }
                    return json.dumps(fallback, ensure_ascii=False, indent=2)
            except Exception:
                pass

            m = re.search(r"(\{[\s\S]*\})", s)
            if m:
                try:
                    parsed = json.loads(m.group(1))
                    return json.dumps(parsed, ensure_ascii=False, indent=2)
                except Exception:
                    pass

            if '"steps"' in s and '{' not in s:
                candidate = '{' + s + '}'
                try:
                    parsed = json.loads(candidate)
                    return json.dumps(parsed, ensure_ascii=False, indent=2)
                except Exception:
                    pass

            return None
        except Exception:
            return None

    def _extract_text_from_response(self, response) -> Optional[str]:
        try:
            try:
                if getattr(response, 'text', None):
                    return response.text
            except Exception:
                pass

            parts = []
            if getattr(response, 'result', None) and getattr(response.result, 'parts', None):
                for p in response.result.parts:
                    if getattr(p, 'text', None):
                        parts.append(p.text)
            elif getattr(response, 'candidates', None):
                for cand in response.candidates:
                    if getattr(cand, 'content', None) and getattr(cand.content, 'parts', None):
                        for p in cand.content.parts:
                            if getattr(p, 'text', None):
                                parts.append(p.text)
                    else:
                        if getattr(cand, 'text', None):
                            parts.append(cand.text)

            return '\n'.join(parts) if parts else None
        except Exception:
            return None

    def chain1_planning(self, user_request: str) -> Dict[str, Any]:
        print('\n' + '=' * 60)
        print('[PHASE 1] PLANNING')
        print('=' * 60)

        prompt = PLANNING_PROMPT.replace('{user_request}', user_request)

        print('\n[SENDING] Sending planning prompt...')

        json_output = None
        try:
            if getattr(self, '_mock_response_text', None):
                json_output = self._mock_response_text
            else:
                response = self.model.generate_content(prompt)
                json_output = self._extract_text_from_response(response)

            if json_output is None:
                raise ValueError('Model response had no accessible text parts')

            print(f"\n[RAW RESPONSE] received ({len(json_output)} characters)")
            preview = (json_output[:1000] + '...') if len(json_output) > 1000 else json_output
            print('\n--- Response preview (first 1000 characters) ---\n')
            print(preview)
            print('\n--- end preview ---\n')

            try:
                plan = json.loads(json_output)
                print('[OK] JSON parsed successfully')
            except json.JSONDecodeError as jde:
                print('[WARN] JSON invalid, attempting to repair...')
                print('\n[DEBUG] JSONDecodeError:', str(jde))
                print('\n[DEBUG] Raw response (repr, first 2000 chars):\n', repr(json_output[:2000]))
                fixed_json = self.fix_json(json_output)
                if fixed_json:
                    try:
                        plan = json.loads(fixed_json)
                        print('[OK] JSON repaired and parsed successfully')
                    except Exception as e:
                        print('[ERROR] Repair parse error:', str(e))
                        # Save raw response for debugging
                        try:
                            with open('last_raw_plan_response.txt', 'w', encoding='utf-8') as fh:
                                fh.write(json_output or '')
                            print("[DEBUG] Raw response saved to 'last_raw_plan_response.txt'")
                        except Exception:
                            pass
                        raise ValueError('JSON repair parse failed; saved raw response to last_raw_plan_response.txt')
                else:
                    try:
                        with open('last_raw_plan_response.txt', 'w', encoding='utf-8') as fh:
                            fh.write(json_output or '')
                        print("[DEBUG] Raw response saved to 'last_raw_plan_response.txt'")
                    except Exception:
                        pass
                    raise ValueError('JSON repair failed; saved raw response to last_raw_plan_response.txt')

            print('\n' + '-' * 60)
            print('[GENERATED PLAN]')
            print('-' * 60)
            print(json.dumps(plan, ensure_ascii=False, indent=2))

            return plan

        except Exception:
            try:
                with open('last_raw_plan_response.txt', 'w', encoding='utf-8') as fh:
                    fh.write(json_output or '')
                print("[DEBUG] Raw response saved to 'last_raw_plan_response.txt'")
            except Exception:
                pass
            traceback.print_exc()
            raise

    def chain2_execution(self, user_request: str, plan: Dict[str, Any]) -> str:
        print('\n' + '=' * 60)
        print('[PHASE 2] EXECUTION')
        print('=' * 60)

        plan_str = json.dumps(plan, ensure_ascii=False, indent=2)
        prompt = EXECUTION_PROMPT.replace('{user_request}', user_request).replace('{plan}', plan_str)

        print('\n[SENDING] Sending execution prompt...')

        guide = None
        try:
            if getattr(self, '_mock_response_text', None):
                guide = self._mock_response_text
            else:
                response = self.model.generate_content(prompt)
                guide = self._extract_text_from_response(response)

            if guide is None:
                raise ValueError('Model response had no accessible text parts for guide')

            print(f"\n[GUIDE RECEIVED] ({len(guide)} characters)")
            print('[OK] Guide generated in Markdown format')

            return guide

        except Exception:
            traceback.print_exc()
            raise

    def generate(self, user_request: str) -> str:
        print('\n[START] Starting guide generation...')
        print(f'Request: {user_request}\n')

        plan = self.chain1_planning(user_request)
        guide = self.chain2_execution(user_request, plan)

        print('\n' + '=' * 60)
        print('[OK] GUIDE READY!')
        print('=' * 60)

        return guide


def main():
    parser = argparse.ArgumentParser(
        description='Fitness Coach - Prompt Chaining',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Example usage:
  python app.py --prompt "30-minute full-body workout with dumbbells"
  python app.py --prompt "Beginner running plan: go from 0 to 5K in 8 weeks" --temperature 0.8
  python app.py --prompt "Quick mobility routine for desk workers" --max_tokens 3000
        ''',
    )

    parser.add_argument('--prompt', type=str, required=True, help='Guide request')
    parser.add_argument('--temperature', type=float, default=0.7, help='Creativity level (0.0-1.0)')
    parser.add_argument('--top_p', type=float, default=0.9, help='Nucleus sampling parameter (0.0-1.0)')
    parser.add_argument('--max_tokens', type=int, default=4096, help='Maximum token count')
    parser.add_argument('--mock', action='store_true', help='Use a mock response from last_raw_plan_response.txt (for testing without API)')

    args = parser.parse_args()

    print('\n' + '=' * 60)
    print('  FITNESS COACH - PROMPT CHAINING')
    print('  Two-step fitness plan generation using Gemini API')
    print('=' * 60)

    try:
        generator = FitnessCoach(temperature=args.temperature, top_p=args.top_p, max_tokens=args.max_tokens)

        if args.mock:
            try:
                with open('last_raw_plan_response.txt', 'r', encoding='utf-8') as fh:
                    mock_text = fh.read()
                generator._mock_response_text = mock_text
                print("[INFO] Loaded mock response from 'last_raw_plan_response.txt'")
            except Exception as e:
                print(f"[WARN] Could not load mock response: {e}")

        guide = generator.generate(args.prompt)

        print('\n' + '=' * 60)
        print('[FINAL GUIDE]')
        print('=' * 60 + '\n')
        print(guide)

        output_file = 'final_fitness_guide.md'
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(guide)
        print(f"\n[Saved] Guide saved to '{output_file}'")

    except KeyboardInterrupt:
        print('\n\n[Cancelled] Operation cancelled by user')
        sys.exit(0)
    except Exception as e:
        print(f"\n[ERROR] Unexpected error: {str(e)}")
        sys.exit(1)


if __name__ == '__main__':
    main()