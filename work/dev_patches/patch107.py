import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))

# explain the generated names that are not plain storage locations
s = open('main.py', encoding='utf-8').read()
old = """             'g_XXXX (global address), Proc_XXXXXX (code address). Event handlers, control names, string',
             'literals and DirectX/VB member names are original.',"""
assert old in s
s = s.replace(old, """             'g_XXXX (global address), Proc_XXXXXX (code address), pN (parameters). Event handlers, control',
             'names, string literals and DirectX/VB member names are original.',
             'Other generated names: rXX = a value the compiler kept in a CPU register (rBX, rSI...);',
             'i, j, k = such register loop counters; idx, idx2 = an array index recovered from a loop that',
             'walked the array with a pointer; var_Scratch / var_1000X = a local the compiler stored in an',
             'unused parameter slot; tmpN = an intermediate result; m_XXX(i).fNN = field at byte offset NN',
             'of a record (Type) array element.',""", 1)
open('main.py', 'w', encoding='utf-8').write(s)
print('patched')
