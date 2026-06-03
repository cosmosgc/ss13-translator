from review_ui.scanner import _join_dm_continuations, extract_dm_strings
text = '''examine_list += "It [LAZYLEN(ingredient_names) \
		? "contains [english_list(ingredient_names)] making a [custom_adjective()]-sized [initial(atom_parent.name)]" \
		: "does not contain any ingredients"]."'''
joined, lm = _join_dm_continuations(text)
print('JOINED:', joined)
print('LINE_MAP:', lm)
print('STRINGS:', extract_dm_strings(joined))
