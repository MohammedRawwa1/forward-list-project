"""
Add search buttons to all courses views in base_handlers.py.
"""
import urllib.parse

with open('handlers/base_handlers.py', 'r', encoding='utf-8') as f:
    content = f.read()

original_len = len(content)
count = 0

# ========= PATTERN 1: Global courses in courses_callback (global, total_count) =========
old1 = (
    "build_courses_page(all_courses, page=page, origin_type='global', total_count=total_courses, is_page=True)\n"
    "                        if not text:\n"
    "                            await safe_edit_message(query, f\"No courses found on page {page}.\", action_key=getattr(query, 'data', None))\n"
    "                            return\n"
    "                        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))\n"
    "                        return"
)
new1 = (
    "build_courses_page(all_courses, page=page, origin_type='global', total_count=total_courses, is_page=True)\n"
    "                        if not text:\n"
    "                            await safe_edit_message(query, f\"No courses found on page {page}.\", action_key=getattr(query, 'data', None))\n"
    "                            return\n"
    "                        # Add Search button for all courses\n"
    "                        try:\n"
    "                            kb = list(reply_markup.inline_keyboard)\n"
    '                            kb.append([InlineKeyboardButton("\\U0001f50d Search", callback_data=f"search_courses::global::{page}")])\n'
    "                            reply_markup = InlineKeyboardMarkup(kb)\n"
    "                        except Exception:\n"
    "                            pass\n"
    "                        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))\n"
    "                        return"
)
n1 = content.count(old1)
if n1:
    content = content.replace(old1, new1, 1)
    count += 1
    print(f"Pattern 1 (global/total_count): replaced {n1} occurrence(s)")

# ========= PATTERN 2: Global courses in courses_callback (global, origin_context=None) =========
old2 = (
    "build_courses_page(all_courses, page=page, origin_type='global', origin_context=None, total_count=total_courses, is_page=True)\n"
    "                        if not text:\n"
    "                            await safe_edit_message(query, f\"No courses found on page {page}.\", action_key=getattr(query, 'data', None))\n"
    "                            return\n"
    "                        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))\n"
    "                        return"
)
new2 = (
    "build_courses_page(all_courses, page=page, origin_type='global', origin_context=None, total_count=total_courses, is_page=True)\n"
    "                        if not text:\n"
    "                            await safe_edit_message(query, f\"No courses found on page {page}.\", action_key=getattr(query, 'data', None))\n"
    "                            return\n"
    "                        # Add Search button for all courses\n"
    "                        try:\n"
    "                            kb = list(reply_markup.inline_keyboard)\n"
    '                            kb.append([InlineKeyboardButton("\\U0001f50d Search", callback_data=f"search_courses::global::{page}")])\n'
    "                            reply_markup = InlineKeyboardMarkup(kb)\n"
    "                        except Exception:\n"
    "                            pass\n"
    "                        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))\n"
    "                        return"
)
n2 = content.count(old2)
if n2:
    content = content.replace(old2, new2, 1)
    count += 1
    print(f"Pattern 2 (global/origin_context): replaced {n2} occurrence(s)")

# ========= PATTERN 3: Category courses in courses_callback (with total_count, store_page_ref) =========
old3 = (
    "build_courses_page(courses, page=page, origin_type='category', category=category, origin_context=origin_ctx, origin_context_page=origin_ctx_page, total_count=total_courses, is_page=True, store_page_ref=True)\n"
    "                        if not text:\n"
    "                            await safe_edit_message(query, f\"No courses found in category '{category}' on page {page}.\", action_key=getattr(query, 'data', None))\n"
    "                            return\n"
    "                        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))"
)
new3 = (
    "build_courses_page(courses, page=page, origin_type='category', category=category, origin_context=origin_ctx, origin_context_page=origin_ctx_page, total_count=total_courses, is_page=True, store_page_ref=True)\n"
    "                        if not text:\n"
    "                            await safe_edit_message(query, f\"No courses found in category '{category}' on page {page}.\", action_key=getattr(query, 'data', None))\n"
    "                            return\n"
    "                        # Add Search button for courses in this category\n"
    "                        try:\n"
    "                            kb = list(reply_markup.inline_keyboard)\n"
    '                            kb.append([InlineKeyboardButton("\\U0001f50d Search", callback_data=f"search_category_courses::{urllib.parse.quote_plus(str(category))}::{page}")])\n'
    "                            reply_markup = InlineKeyboardMarkup(kb)\n"
    "                        except Exception:\n"
    "                            pass\n"
    "                        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))"
)
n3 = content.count(old3)
if n3:
    content = content.replace(old3, new3, 1)
    count += 1
    print(f"Pattern 3 (category/total_count): replaced {n3} occurrence(s)")

# ========= PATTERN 4: Category courses in courses_callback (minimal, no page_ref) =========
old4 = (
    "build_courses_page(courses, page=page, origin_type='category', category=category, origin_context=origin_ctx, origin_context_page=origin_ctx_page)\n"
    "                    if not text:\n"
    "                        await safe_edit_message(query, f\"No courses found in category '{category}' on page {page}.\", action_key=getattr(query, 'data', None))\n"
    "                        return\n"
    "                    await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))"
)
new4 = (
    "build_courses_page(courses, page=page, origin_type='category', category=category, origin_context=origin_ctx, origin_context_page=origin_ctx_page)\n"
    "                    if not text:\n"
    "                        await safe_edit_message(query, f\"No courses found in category '{category}' on page {page}.\", action_key=getattr(query, 'data', None))\n"
    "                        return\n"
    "                    # Add Search button for courses in this category\n"
    "                    try:\n"
    "                        kb = list(reply_markup.inline_keyboard)\n"
    '                        kb.append([InlineKeyboardButton("\\U0001f50d Search", callback_data=f"search_category_courses::{urllib.parse.quote_plus(str(category))}::{page}")])\n'
    "                        reply_markup = InlineKeyboardMarkup(kb)\n"
    "                    except Exception:\n"
    "                        pass\n"
    "                    await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))"
)
n4 = content.count(old4)
if n4:
    content = content.replace(old4, new4, 1)
    count += 1
    print(f"Pattern 4 (category/minimal): replaced {n4} occurrence(s)")

# ========= PATTERN 5: Coach courses in courses_callback =========
old5 = (
    "build_courses_page(coach_courses, page=page, origin_type='coach', category=coach_name, origin_context=None, total_count=total_courses, is_page=True, store_page_ref=True)\n"
    "                        if not text:\n"
    "                            await safe_edit_message(query, f\"No courses found for coach '{coach_name}' on page {page}.\", action_key=getattr(query, 'data', None))\n"
    "                            return\n"
    "                        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))"
)
new5 = (
    "build_courses_page(coach_courses, page=page, origin_type='coach', category=coach_name, origin_context=None, total_count=total_courses, is_page=True, store_page_ref=True)\n"
    "                        if not text:\n"
    "                            await safe_edit_message(query, f\"No courses found for coach '{coach_name}' on page {page}.\", action_key=getattr(query, 'data', None))\n"
    "                            return\n"
    "                        # Add Search button for courses by this coach\n"
    "                        try:\n"
    "                            kb = list(reply_markup.inline_keyboard)\n"
    '                            kb.append([InlineKeyboardButton("\\U0001f50d Search", callback_data=f"search_courses::coach::{urllib.parse.quote_plus(str(coach_name))}::{page}")])\n'
    "                            reply_markup = InlineKeyboardMarkup(kb)\n"
    "                        except Exception:\n"
    "                            pass\n"
    "                        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))"
)
n5 = content.count(old5)
if n5:
    content = content.replace(old5, new5, 1)
    count += 1
    print(f"Pattern 5 (coach): replaced {n5} occurrence(s)")

# ========= PATTERN 6: items-based courses callback (dynamic origin_type) =========
old6 = (
    "build_courses_page(items, page=page, origin_type=origin_type, category=category, origin_context=origin_ctx, origin_context_page=origin_ctx_page, total_count=total_count, is_page=True, store_page_ref=False)\n"
    "                if not text:\n"
    "                    await safe_edit_message(query, \"No courses found.\", action_key=getattr(query, 'data', None))\n"
    "                    return\n"
    "                await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))"
)
new6 = (
    "build_courses_page(items, page=page, origin_type=origin_type, category=category, origin_context=origin_ctx, origin_context_page=origin_ctx_page, total_count=total_count, is_page=True, store_page_ref=False)\n"
    "                if not text:\n"
    "                    await safe_edit_message(query, \"No courses found.\", action_key=getattr(query, 'data', None))\n"
    "                    return\n"
    "                # Add Search button\n"
    "                try:\n"
    "                    kb = list(reply_markup.inline_keyboard)\n"
    "                    if origin_type == 'category' and category:\n"
    '                        kb.append([InlineKeyboardButton("\\U0001f50d Search", callback_data=f"search_category_courses::{urllib.parse.quote_plus(str(category))}::{page}")])\n'
    "                    else:\n"
    '                        kb.append([InlineKeyboardButton("\\U0001f50d Search", callback_data=f"search_courses::{origin_type}::{page}")])\n'
    "                    reply_markup = InlineKeyboardMarkup(kb)\n"
    "                except Exception:\n"
    "                    pass\n"
    "                await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))"
)
n6 = content.count(old6)
if n6:
    content = content.replace(old6, new6, 1)
    count += 1
    print(f"Pattern 6 (items-based): replaced {n6} occurrence(s)")

# ========= PATTERN 7: list_courses - /courses command handler =========
old7 = (
    "text, reply_markup = build_courses_page(all_courses, page=page, origin_type='global', origin_context=None, total_count=total_courses, is_page=True)\n"
    "            if not text:\n"
    "                await update.message.reply_text(\"No courses available.\")\n"
    "                return\n"
    "            msg = await update.message.reply_text(text, reply_markup=reply_markup)"
)
new7 = (
    "text, reply_markup = build_courses_page(all_courses, page=page, origin_type='global', origin_context=None, total_count=total_courses, is_page=True)\n"
    "            if not text:\n"
    "                await update.message.reply_text(\"No courses available.\")\n"
    "                return\n"
    "            # Add Search button for all courses\n"
    "            try:\n"
    "                kb = list(reply_markup.inline_keyboard)\n"
    '                kb.append([InlineKeyboardButton("\\U0001f50d Search", callback_data=f"search_courses::global::{page}")])\n'
    "                reply_markup = InlineKeyboardMarkup(kb)\n"
    "            except Exception:\n"
    "                pass\n"
    "            msg = await update.message.reply_text(text, reply_markup=reply_markup)"
)
n7 = content.count(old7)
if n7:
    content = content.replace(old7, new7, 1)
    count += 1
    print(f"Pattern 7 (list_courses): replaced {n7} occurrence(s)")

# ========= Summary =========
print(f"\nTotal edits applied: {count}")
print(f"File size: {original_len} -> {len(content)} chars")

if count > 0:
    with open('handlers/base_handlers.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("File saved successfully!")
else:
    print("No changes were made!")
